#!/usr/bin/env bash
#
# Is Avora's automatic task creation actually running? Read-only.
#
# WHY THIS EXISTS
# ---------------
# Three features now create tasks without anybody typing them: recurrence,
# playbooks, and untracked-work detection. Every one of them can be fully
# deployed and still do nothing, for a different reason, and none of those
# reasons raises an error:
#
#   * recurrence  - needs its own Coolify resource AND a flag. It has never
#                   run: `worker_heartbeats` has no row for it at all.
#   * playbooks   - live INSIDE worker/forms_scheduler.py, which is a SEPARATE
#                   Coolify resource. Deploying the API does not update it, so
#                   the tables and the UI arrive and the sweeps never fire.
#                   Measured: the API was 10 days newer than that worker.
#   * detection   - same worker, plus a flag that is off by default.
#
# That pattern has already cost this project seven weeks of silent celebration
# emails and a recurrence feature that shipped without a process. This script
# checks each condition and says which one is missing.
#
# USAGE:  bash deploy/verify-automation.sh
set -uo pipefail

LIVE_DB_HOST=eteddkdrtn7slsrjnjpysbut
ok() { printf '  \033[32mOK\033[0m    %s\n' "$1"; }
no() { printf '  \033[31mMISS\033[0m  %s\n' "$1"; FAILED=$((FAILED + 1)); }
note() { printf '        %s\n' "$1"; }
FAILED=0

echo "==> Finding the live API container"
API=""
for c in $(docker ps --format '{{.Names}}'); do
  url="$(docker exec "$c" printenv DATABASE_URL 2>/dev/null || true)"
  case "$url" in *"@${LIVE_DB_HOST}:"*) ;; *) continue ;; esac
  case "$(docker inspect -f '{{json .Config.Cmd}}' "$c")" in *uvicorn*) ;; *) continue ;; esac
  API="$c"
done
[ -n "$API" ] || { echo "FATAL: no live Avora API container found" >&2; exit 1; }
API_IMAGE="$(docker inspect -f '{{.Image}}' "$API")"
echo "    api=$API"

echo
echo "==> Schema"
psql_q() { docker exec -i "$LIVE_DB_HOST" psql -U pms_app -d pms -At -c "$1" 2>/dev/null; }
[ "$(psql_q "select count(*) from pg_tables where tablename='playbooks'")" = "1" ] \
  && ok "playbooks tables present" \
  || { no "playbooks tables missing"; note "the migration has not run: deploy the backend"; }
[ "$(psql_q "select count(*) from information_schema.columns where table_name='tasks' and column_name='auto_detected'")" = "1" ] \
  && ok "tasks.auto_detected present" \
  || no "tasks.auto_detected missing - deploy the backend"

echo
echo "==> The worker that runs playbooks and detection"
# THE check this script exists for. The forms scheduler is its own Coolify
# resource; an API deploy leaves it on its old image, so new sweep code never
# runs even though everything else looks installed.
FORMS=""
for c in $(docker ps --format '{{.Names}}'); do
  case "$(docker inspect -f '{{join .Config.Cmd " "}}' "$c" 2>/dev/null)" in
    *forms_scheduler*) FORMS="$c" ;;
  esac
done
if [ -z "$FORMS" ]; then
  no "no forms_scheduler container is running"
  note "playbooks and detection cannot fire at all"
else
  # Checked by CODE, not by image id. Comparing image hashes was wrong: Coolify
  # builds each resource separately, so identical source yields different
  # hashes and the check cried wolf on a correct deploy.
  #
  # It cried wolf while hiding the real problem, which is worse. The workers are
  # Coolify apps with build pack `dockerimage`: they do not track git at all,
  # they pull a tag from the local registry. Redeploying the resource re-pulls
  # the SAME stale image. Only `deploy/build-workers.sh` moves new code in, and
  # the only honest test of that is whether the running worker contains it.
  # `grep -q`, not `grep -c`: grep EXITS 1 when the count is zero, so a
  # `grep -c ... || echo 0` prints the count AND the fallback - "0\n0", which
  # aborts the test with "integer expected" rather than reporting anything.
  if docker exec "$FORMS" \
      grep -q "_detect_untracked" /app/worker/forms_scheduler.py 2>/dev/null; then
    ok "forms_scheduler is running current sweep code"
  else
    no "forms_scheduler is running STALE code - the sweeps are not in it"
    note "the workers pull from the local registry and never track git, so a"
    note "Coolify redeploy alone re-pulls the same image. Fix:"
    note "  bash deploy/build-workers.sh     # build + push to localhost:5000"
    note "  then redeploy the forms-scheduler resource in Coolify"
  fi
fi

echo
echo "==> Liveness (a worker is only alive if it SAYS so)"
for name in forms task-recurrence; do
  # `head -1` and the numeric guard are not paranoia: psql can hand back more
  # than the one line this expects (a notice, a second row under
  # schema-per-workspace), and `[ "0\n0" -lt ... ]` aborts the check with
  # "integer expected" instead of reporting anything useful.
  age="$(psql_q "select extract(epoch from now() - last_beat_at)::int from worker_heartbeats where name='$name' order by last_beat_at desc limit 1" | tr -d '[:space:]' | head -1)"
  case "$age" in ''|*[!0-9]*) age="" ;; esac
  if [ -z "$age" ]; then
    no "$name has NEVER reported"
    [ "$name" = "task-recurrence" ] && note "it has no Coolify resource: see deploy/enable-task-recurrence.sh"
  elif [ "$age" -lt 7200 ]; then
    ok "$name beat ${age}s ago"
  else
    no "$name last beat ${age}s ago - it is stuck or dead"
  fi
done

echo
echo "==> Flags (checked on the container that READS each one)"
# Not on the API. Each flag is read by the worker that acts on it, and they are
# different containers: TASK_RECURRENCE_ENABLED is set on the recurrence
# worker, UNTRACKED_DETECTION_ENABLED on whatever runs forms_scheduler. An
# earlier version read both off the API and reported a correctly-enabled
# feature as MISS.
flag_on() {  # $1 = flag, $2 = container (may be empty)
  [ -n "$2" ] || { no "$1: no container running to read it"; return; }
  val="$(docker exec "$2" printenv "$1" 2>/dev/null || true)"
  case "${val:-unset}" in
    true|True|1) ok "$1=$val  (on $2)" ;;
    *) no "$1 is ${val:-unset} on $2 - that feature is idle by design" ;;
  esac
}
RECUR=""
for c in $(docker ps --format '{{.Names}}'); do
  case "$(docker inspect -f '{{join .Config.Cmd " "}}' "$c" 2>/dev/null)" in
    *task_recurrence_scheduler*) RECUR="$c" ;;
  esac
done
flag_on TASK_RECURRENCE_ENABLED "$RECUR"
flag_on UNTRACKED_DETECTION_ENABLED "$FORMS"

echo
echo "==> What the data says it would actually do"
note "published playbooks:      $(psql_q "select count(*) from playbooks where status='PUBLISHED'" || echo n/a)"
note "armed recurring series:   $(psql_q "select count(*) from tasks where cadence::text in ('DAILY','WEEKLY','MONTHLY') and recurrence_active")"
note "tasks made by recurrence: $(psql_q "select count(*) from tasks where series_id is not null")"
note "tasks made by a playbook: $(psql_q "select count(*) from tasks where playbook_run_id is not null" || echo n/a)"
note "tasks made by detection:  $(psql_q "select count(*) from tasks where auto_detected" || echo n/a)"
note "matchable work entities:  $(psql_q "select count(*) from work_entities where is_active")"

echo
if [ "$FAILED" -eq 0 ]; then
  echo "All checks passed. Zeroes above are a DATA question, not a deploy one:"
  echo "nothing recurs until somebody ticks 'keep creating the next one', and"
  echo "no playbook fires until one is published."
else
  echo "$FAILED check(s) failed. Each one above says what to do."
fi
exit 0
