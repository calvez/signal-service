#!/usr/bin/env bash
# Nightly off-server backup of the signal log. Runs inside the "trader" container.
# Needs /root/.restic-env (mode 600) with:
#   RESTIC_REPOSITORY=sftp:uXXXXXX@uXXXXXX.your-storagebox.de:/trader-restic
#   RESTIC_PASSWORD=<long random string — keep a copy outside this server>
#   DB_PATH=/root/signal-service/data/signal.db
set -euo pipefail
set -a; . /root/.restic-env; set +a

STAGE=/var/backups/signal
mkdir -p "$STAGE"
# .backup gives a consistent copy even while the service writes (WAL mode).
sqlite3 "$DB_PATH" ".backup '$STAGE/signal.db'"
cp /root/signal-service/config.yaml "$STAGE/" 2>/dev/null || true

restic backup --quiet --tag nightly "$STAGE"
restic forget --quiet --tag nightly --keep-daily 14 --keep-weekly 8 --keep-monthly 12 --prune
echo "backup ok $(date -u +%FT%TZ)"
