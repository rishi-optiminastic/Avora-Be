#!/usr/bin/env bash
#
# Bring Avora's background workers onto the LIVE database, and apply any
# migration the last deploy shipped code for but never ran.
#
# WHAT WAS WRONG (5 Nov 2026)
# ---------------------------
# Two complete copies of Avora were running:
#
#   * Coolify  - api + 3 workers, on database `eteddkdrtn7slsrjnjpysbut`
#                (29 employees, current).  Network: coolify
#   * deploy_* - a legacy compose stack with its own api, its own database
#                `deploy-db-1` (10 employees, newest task 3 August), and the
#                other 5 schedulers.        Network: deploy_avora
#
# The two networks cannot reach each other, so the five schedulers that matter
# - eod, payroll, task-escalation, celebrations, forms - had been running
# against a three-month-old snapshot. They wrote heartbeats there, which is why
# `deploy-db-1.worker_heartbeats` had rows while the app's Status page showed
# "8 of 8 NEVER RUN": it reads the live database, where the table was empty.
#
# Nothing was crashing. The workers were working perfectly on the wrong data.
#
# WHAT THIS DOES
# --------------
# 1. Applies pending migrations in the CURRENT api container. Coolify's
#    pre_deployment_command runs in the OLD container, so `alembic upgrade`
#    there no-ops and ships new code onto an old schema - which is how the Room
#    went live with its tables missing.
# 2. Runs the five missing schedulers from the api's OWN image, on the coolify
#    network, inheriting its env - so they use the same code and the same
#    database as the API by construction, not by a copied connection string.
# 3. Stops the legacy schedulers so they stop acting on August data.
#
# It does NOT delete `deploy-db-1`. That snapshot may be the only copy of that
# state, and deleting it is not this script's call.
#
# USAGE:  bash deploy/make-workers-live.sh
set -euo pipefail

NETWORK=coolify
# The five with no Coolify resource. circle-documents, ocr and autocheckout
# already run there correctly and are deliberately left alone.
MISSING=(
  "eod:worker.eod_scheduler"
  "payroll:worker.payroll_scheduler"
  "celebrations:worker.celebrations_scheduler"
  "forms:worker.forms_scheduler"
  "task-escalation:worker.task_escalation_scheduler"
)

# The Avora database, by the hostname the API connects to. Matching on this
# rather than on "runs uvicorn" matters: this box also runs circle-be-api,
# project-o2-api, idsync-api and raised-api, every one of them a uvicorn
# process. A looser match could have pointed `alembic upgrade head` at another
# product's database.
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
  echo "FATAL: expected exactly 1 Avora API container, found $#: $CANDIDATES" >&2
  echo "Refusing to guess - a migration must not run against the wrong app." >&2
  exit 1
fi
API="$1"
IMAGE="$(docker inspect -f '{{.Image}}' "$API")"
echo "    api=$API"
echo "    image=$IMAGE"

echo "==> Confirming the target database before touching it"
docker exec "$API" python -c "
import asyncio, os
from sqlalchemy import text
from app.db.session import SessionFactory
async def main():
    async with SessionFactory() as s:
        n = (await s.execute(text('select count(*) from employees where is_active'))).scalar_one()
        print(f'    active employees on target: {n}')
        assert n > 20, f'refusing: {n} employees looks like the stale snapshot, not live'
asyncio.run(main())
"

# The image was built before the migration was fixed, so the buggy copy is
# baked into it. Patch the file in place rather than waiting on a rebuild: the
# next Coolify deploy pulls the corrected version from git and this becomes a
# no-op. Harmless to repeat.
FIXED_MIGRATION=/opt/avora/be/deploy/626c7efe6527_room_messages_and_event_rsvps.py
if [ -f "$FIXED_MIGRATION" ]; then
  echo "==> Patching the room migration inside the container"
  docker cp "$FIXED_MIGRATION" \
    "${API}:/app/migrations/versions/626c7efe6527_room_messages_and_event_rsvps.py"
fi

echo "==> Applying pending migrations in the CURRENT container"
docker exec "$API" alembic upgrade head

echo "==> Confirming the room tables landed"
docker exec "$API" python -c "
import asyncio
from sqlalchemy import text
from app.db.session import SessionFactory
async def main():
    async with SessionFactory() as s:
        for t in ('room_messages', 'event_rsvps'):
            got = (await s.execute(text(f\"select to_regclass('public.{t}')\"))).scalar_one()
            assert got is not None, f'{t} was not created'
            print(f'    {t} ok')
asyncio.run(main())
"

echo "==> Starting the five missing schedulers on the live database"
for entry in "${MISSING[@]}"; do
  name="${entry%%:*}"
  module="${entry##*:}"
  container="avora-worker-${name}"

  docker rm -f "$container" >/dev/null 2>&1 || true
  # --volumes-from is deliberately NOT used: a scheduler needs the api's
  # environment, not its filesystem.
  docker run -d \
    --name "$container" \
    --network "$NETWORK" \
    --restart unless-stopped \
    $(docker inspect -f '{{range .Config.Env}}--env {{.}} {{end}}' "$API") \
    "$IMAGE" python -m "$module"
  echo "    started $container -> $module"
done

echo "==> Stopping the legacy schedulers (reversible: docker start <name>)"
for c in $(docker ps --format '{{.Names}}' | grep -E '^deploy-(scheduler|worker|api)-' || true); do
  docker stop "$c" >/dev/null && echo "    stopped $c"
done

echo
echo "==> Verify in ~2 minutes:"
echo "docker exec \$(docker ps --format '{{.Names}}' | grep -m1 -v deploy- | head -1) true"
echo "Then open /dashboard/admin/status - the workers should turn green as each"
echo "completes its first tick, and /dashboard should show The Room."
