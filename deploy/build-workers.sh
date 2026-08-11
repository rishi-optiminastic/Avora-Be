#!/usr/bin/env bash
#
# Build + push the four Avora worker images to the VPS's local registry, so a
# Coolify redeploy pulls fresh code into the background workers.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# On the Coolify box the four background workers - the eod / payroll /
# auto-checkout schedulers plus the OCR worker - are Coolify applications with
# build pack `dockerimage`. They do not track git at all: each one simply pulls
# a tag out of the local registry. A Coolify deploy of `avora-api` rebuilds the
# API from git, but leaves the workers on whatever was last pushed here. So
# after any backend change the workers keep running stale code until this script
# runs and the four worker resources are redeployed.
#
# WHY NOT A COMPOSE FILE
# ----------------------
# This replaces the older `docker-compose.workers.yml`, which could not work:
#   1. It pushed `avora-pms-api` / `avora-ocr-worker`, but Coolify pulls
#      `avora-worker-{eod,payroll,autocheckout,ocr}`. The pushes landed on tags
#      nothing referenced, so a "successful" run changed nothing on the box.
#   2. The three schedulers share one codebase and differ ONLY by their start
#      command, and that command is BAKED INTO EACH IMAGE (Coolify sets no
#      start-command override for them). Compose's `command:` is a runtime
#      override applied to containers, so it cannot bake a per-image CMD.
# Both points are why this is a script that derives one image per worker.
#
# BASH, NOT ZSH
# -------------
# Run this with bash. Under zsh, `$name:latest` is parsed as the `:l` (lowercase)
# parameter modifier followed by a literal `atest`, which silently rewrites
# `avora-worker-eod:latest` into `avora-worker-eodatest`. The registry then grows
# junk repos while the real tags stay stale.
#
# USAGE (on the VPS):
#   bash deploy/build-workers.sh            # builds from a clean clone of main
#   REGISTRY=localhost:5000 bash deploy/build-workers.sh
# then redeploy the four worker resources in Coolify so they re-pull the tags.

set -euo pipefail

REGISTRY="${REGISTRY:-localhost:5000}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Each scheduler is the API image with a different module as its CMD. The names
# on the left MUST match `docker_registry_image_name` on the Coolify apps.
SCHEDULERS=(
  "eod:worker.eod_scheduler"
  "payroll:worker.payroll_scheduler"
  "autocheckout:worker.auto_checkout_scheduler"
)

BASE_IMAGE="avora-worker-base:build"
STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT

echo "==> Building base image from ${REPO_ROOT}"
docker build -t "$BASE_IMAGE" -f "${REPO_ROOT}/Dockerfile" "$REPO_ROOT"

for entry in "${SCHEDULERS[@]}"; do
  name="${entry%%:*}"
  module="${entry##*:}"
  target="${REGISTRY}/avora-worker-${name}:latest"

  # A one-line derived image is the only way to bake a per-worker CMD while
  # keeping all three schedulers byte-identical to the API build below it.
  printf 'FROM %s\nCMD ["python", "-m", "%s"]\n' "$BASE_IMAGE" "$module" \
    > "${STAGE_DIR}/Dockerfile.${name}"

  echo "==> Building ${target}"
  docker build -t "$target" -f "${STAGE_DIR}/Dockerfile.${name}" "$STAGE_DIR"
done

OCR_TARGET="${REGISTRY}/avora-worker-ocr:latest"
echo "==> Building ${OCR_TARGET}"
docker build -t "$OCR_TARGET" -f "${REPO_ROOT}/worker/Dockerfile" "${REPO_ROOT}/worker"

echo
echo "==> Verifying each image has the expected start command"
for entry in "${SCHEDULERS[@]}"; do
  name="${entry%%:*}"
  module="${entry##*:}"
  target="${REGISTRY}/avora-worker-${name}:latest"
  actual="$(docker inspect "$target" --format '{{json .Config.Cmd}}')"
  expected="[\"python\",\"-m\",\"${module}\"]"
  if [ "$actual" != "$expected" ]; then
    echo "FATAL: ${target} has CMD ${actual}, expected ${expected}" >&2
    exit 1
  fi
  echo "    ${target} -> ${actual}"
done
echo "    ${OCR_TARGET} -> $(docker inspect "$OCR_TARGET" --format '{{json .Config.Cmd}}')"

echo
echo "==> Pushing"
for name in eod payroll autocheckout ocr; do
  target="${REGISTRY}/avora-worker-${name}:latest"
  docker push "$target" | tail -1
done

echo
echo "Done. Now redeploy these four resources in Coolify so they re-pull :latest —"
echo "avora-scheduler-eod, avora-scheduler-payroll, avora-scheduler-autocheckout,"
echo "avora-worker-ocr. Deploy avora-api FIRST: it runs 'alembic upgrade heads',"
echo "and workers on new code need the new schema already applied."
