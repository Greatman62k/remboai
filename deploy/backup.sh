#!/usr/bin/env bash
# Nightly database backup, 14 days kept. Install:  echo '17 2 * * * root /opt/remboai/deploy/backup.sh' > /etc/cron.d/remboai-backup
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
DEST=${BACKUP_DIR:-/var/backups/remboai}
mkdir -p "$DEST"
FILE="$DEST/rembo-$(date +%F-%H%M).sql.gz"
docker compose -f deploy/docker-compose.prod.yml --env-file "${COMPOSE_ENV_FILE:-.env}" exec -T db pg_dump -U rembo --no-owner rembo | gzip > "$FILE"
[ -s "$FILE" ] || { echo "backup is empty, removing"; rm -f "$FILE"; exit 1; }
find "$DEST" -name 'rembo-*.sql.gz' -mtime +14 -delete
echo "backup written: $FILE"
