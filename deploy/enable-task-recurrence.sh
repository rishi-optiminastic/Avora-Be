#!/usr/bin/env bash
#
# Switch on auto task creation, in the only order that is safe.
#
# WHY A SCRIPT AND NOT A CHECKBOX
# -------------------------------
# `tasks.cadence` shipped years ago as a LABEL nothing acted on, so people
# used it to describe work rather than to schedule it. On the live database
# the rows carrying a recurring cadence include "Induction - Aditi",
# "Send documents to Shalu for Aditi" and "Create monthly reports - sep":
# finished, one-off jobs named after the person or month they belonged to.
#
# They become instructions the moment a scheduler reads them. Measured on
# live data before this was written: 117 tasks on the first sweep, 92 of them
# reviving series nobody had touched in over a month.
#
# So the order is not optional:
#
#   1. migration a1c3e5f7b9d4 pauses every series that predates recurrence
#      ever running, making it opt-in
#   2. ONLY THEN does the worker start with the flag on
#
# Reversed, the first tick fires before the pause lands and the damage is
# already in people's task lists. This script enforces the order, shows the
# blast radius, and asks before it switches anything on.
#
# USAGE:  bash deploy/enable-task-recurrence.sh [--yes]
set -euo pipefail

ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

NETWORK=coolify
WORKER=avora-worker-task-recurrence
REVISION=a1c3e5f7b9d4
# Match on the database hostname, not on "runs uvicorn": this box also runs
# circle-be-api, idsync-api and others, every one of them a uvicorn process.
LIVE_DB_HOST=eteddkdrtn7slsrjnjpysbut

echo "==> Finding the live Avora API container"
CANDIDATES=""
for c in $(docker ps --format '{{.Names}}'); do
  url="$(docker exec "$c" printenv DATABASE_URL 2>/dev/null || true)"
  case "$url" in *"@${LIVE_DB_HOST}:"*) ;; *) continue ;; esac
  case "$(docker inspect -f '{{json .Config.Cmd}}' "$c")" in *uvicorn*) ;; *) continue ;; esac
  CANDIDATES="$CANDIDATES $c"
done
set -- $CANDIDATES
if [ "$#" -ne 1 ]; then
  echo "FATAL: expected exactly 1 Avora API container, found $#:$CANDIDATES" >&2
  echo "Refusing to guess - this must not act on another product's database." >&2
  exit 1
fi
API="$1"
IMAGE="$(docker inspect -f '{{.Image}}' "$API")"
echo "    api=$API"

echo "==> Confirming this is the live database, not the August snapshot"
docker exec "$API" python -c "
import asyncio
from sqlalchemy import text
from app.db.session import SessionFactory
async def main():
    async with SessionFactory() as s:
        n = (await s.execute(text('select count(*) from employees where is_active'))).scalar_one()
        print(f'    active employees on target: {n}')
        assert n > 20, f'refusing: {n} employees looks like the stale snapshot'
asyncio.run(main())
"

# Coolify's pre_deployment_command runs in the OLD container, so an
# 'alembic upgrade' there no-ops and ships new code onto an old schema.
# Running it HERE, in the container that is actually serving, is the fix.
echo "==> Applying migrations in the CURRENT container"
docker exec "$API" alembic upgrade head

# HARD INTERLOCK. Not a warning - a refusal.
#
# Checked by the marker TABLE, not by alembic_version: that holds only the
# current head, so once another migration lands on top, '$REVISION' is no
# longer in it and a revision check would silently start passing for the
# wrong reason. The backup table is created by the upgrade and dropped only
# by the downgrade, so its presence is durable proof the pause actually ran.
#
# Starting the worker without it is the one genuinely destructive outcome
# here: 117 tasks into real people's lists, reviving work abandoned months
# ago, with no undo.
echo "==> Verifying the pause migration really ran (hard requirement)"
docker exec "$API" python -c "
import asyncio, sys
from sqlalchemy import text
from app.db.session import SessionFactory
async def main():
    async with SessionFactory() as s:
        at = (await s.execute(text('select version_num from alembic_version'))).scalars().all()
        print(f'    schema at: {at}')
        marker = (await s.execute(text(
            \"select to_regclass('_recurrence_pause_$REVISION')\"
        ))).scalar_one()
        if marker is None:
            print()
            print('    FATAL: migration $REVISION has not run on this database.')
            print('    Every pre-existing recurring series is still armed, and')
            print('    starting the worker now would create ~117 tasks from work')
            print('    people abandoned months ago. There is no undo for that.')
            print()
            print('    The deployed image probably predates the migration:')
            print('    deploy the backend first, then run this again.')
            sys.exit(1)
        n = (await s.execute(text(
            'select count(*) from _recurrence_pause_$REVISION'
        ))).scalar_one()
        print(f'    pause migration confirmed: {n} legacy series made opt-in')
asyncio.run(main())
"

echo "==> Measuring what the first sweep would do (reads only)"
DUE="$(docker exec "$API" python -c "
import asyncio
from sqlalchemy import text
from app.db.session import SessionFactory
Q = '''
select count(*) from tasks
 where cadence::text in ('DAILY','WEEKLY','MONTHLY')
   and recurrence_active
   and due_date is not null
   and series_id is null
   and due_date::date < (now() at time zone 'Asia/Kolkata')::date
'''
async def main():
    async with SessionFactory() as s:
        print((await s.execute(text(Q))).scalar_one())
asyncio.run(main())
" | tr -d '[:space:]')"

echo "    series that would generate a task on the first tick: $DUE"
if [ "$DUE" -gt 10 ] && [ "$ASSUME_YES" -eq 0 ]; then
  echo
  echo "    That is more than a handful. Each one lands in somebody's task list"
  echo "    and notifies them. If you did not expect this many, stop and look at"
  echo "    them first - the pause migration may not have run."
  echo
  printf "    Type 'yes' to switch on anyway: "
  read -r reply
  [ "$reply" = "yes" ] || { echo "    Aborted. Nothing was switched on."; exit 1; }
fi

echo "==> Starting the worker on the live database"
docker rm -f "$WORKER" >/dev/null 2>&1 || true
# From the API's OWN image and env, so the code and the database match by
# construction rather than by a copied connection string. ONE instance:
# two would race on the same series and could double-create.
# Env through a FILE, not a splatted command line.
#
# This was `$(docker inspect -f '{{range .Config.Env}}--env {{.}} {{end}}')`,
# copied from make-workers-live.sh. It is unquoted command substitution, so the
# shell word-splits every value: one env var holding a space is enough to shift
# every later argument along and make docker read a fragment as the image name.
# On this box it failed with
#
#     invalid reference format: repository name (library/PRIVATE) must be lowercase
#
# because a value contains "... PRIVATE ...". `--env-file` takes everything
# after the first `=` as the value, so spaces are safe.
#
# The file holds live secrets: 0600, and removed on exit even if this fails.
ENV_FILE="$(mktemp)"
chmod 600 "$ENV_FILE"
trap 'rm -f "$ENV_FILE"' EXIT
# One var per line; keep only well-formed KEY=... lines, because a value with
# an embedded newline would otherwise become a junk line docker rejects.
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$API" \
  | grep -E '^[A-Za-z_][A-Za-z0-9_]*=' > "$ENV_FILE"
echo "    passing $(wc -l < "$ENV_FILE" | tr -d ' ') environment variables"

docker run -d \
  --name "$WORKER" \
  --network "$NETWORK" \
  --restart unless-stopped \
  --env-file "$ENV_FILE" \
  --env TASK_RECURRENCE_ENABLED=true \
  "$IMAGE" python -m worker.task_recurrence_scheduler >/dev/null

echo "==> Waiting for the first tick to report liveness"
for _ in $(seq 1 24); do
  sleep 5
  if docker exec "$API" python -c "
import asyncio, sys
from sqlalchemy import text
from app.db.session import SessionFactory
async def main():
    async with SessionFactory() as s:
        r = (await s.execute(text(
            \"select 1 from worker_heartbeats where name = 'task-recurrence'\"
        ))).first()
        sys.exit(0 if r else 1)
asyncio.run(main())
" 2>/dev/null; then
    echo "    heartbeat recorded"
    break
  fi
done

echo "==> What it did"
docker logs --tail 20 "$WORKER" 2>&1 | sed 's/^/    /'
docker exec "$API" python -c "
import asyncio
from sqlalchemy import text
from app.db.session import SessionFactory
async def main():
    async with SessionFactory() as s:
        n = (await s.execute(text('select count(*) from tasks where series_id is not null'))).scalar_one()
        print(f'    tasks now belonging to a series: {n}')
asyncio.run(main())
"
echo
echo "Done. Create a Coolify resource for this worker when you get a chance -"
echo "this container is invisible to Coolify, and the Status page is the only"
echo "thing that will notice if it dies."
