#!/usr/bin/env bash
# Nightly Postgres backup for the self-hosted `db` container.
#
# Dumps the database to ./backups/ as a gzipped custom-format archive and prunes
# anything older than RETENTION_DAYS. Restore with:
#   gunzip -c backups/pms-YYYY-MM-DD_HHMMSS.dump.gz \
#     | docker compose -f docker-compose.prod.yml --env-file .env.prod \
#         exec -T db pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean
#
# Install as a daily cron (run `crontab -e` on the VPS) — adjust the path:
#   15 2 * * * cd /opt/avora/be/deploy && ./backup.sh >> backups/backup.log 2>&1
set -euo pipefail

cd "$(dirname "$0")"

# Load POSTGRES_USER / POSTGRES_DB from the prod env file.
set -a
# shellcheck disable=SC1091
source .env.prod
set +a

RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
COMPOSE="docker compose -f docker-compose.prod.yml --env-file .env.prod"
STAMP="$(date +%Y-%m-%d_%H%M%S)"
OUT_DIR="./backups"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/pms-${STAMP}.dump.gz"

echo "[$(date -Is)] dumping ${POSTGRES_DB} -> ${OUT}"
$COMPOSE exec -T db pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc | gzip > "$OUT"

# Prune old dumps.
find "$OUT_DIR" -name 'pms-*.dump.gz' -type f -mtime "+${RETENTION_DAYS}" -delete
echo "[$(date -Is)] done; kept last ${RETENTION_DAYS} days"
