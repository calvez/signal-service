#!/usr/bin/env bash
# Deploy the working copy to /opt/signal-service and (re)start the service.
# Run as root INSIDE the trader container, from the repo:  deploy/deploy.sh
#
# The service runs as the unprivileged user "signal", which cannot read /root, so the code is
# copied to /opt/signal-service. .env, config.yaml and data/ there are never overwritten.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/signal-service

id signal >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/signal --shell /usr/sbin/nologin signal
install -d -o signal -g signal "$DEST" "$DEST/data"

[ -f "$DEST/.env" ] || { echo "Missing $DEST/.env: copy .env.example there, fill it in, chmod 600."; exit 1; }

# Code only (no rsync: it hangs on exit inside the container on this host). .env, config.yaml,
# data/ and .venv in $DEST are left alone.
CODE_DIRS="app scripts prompts docs deploy mt5 tests"
CODE_FILES="pyproject.toml uv.lock .python-version config.example.yaml .env.example README.md CLAUDE.md"
for d in $CODE_DIRS; do rm -rf "${DEST:?}/$d"; done
(cd "$SRC" && tar --exclude=__pycache__ -cf - $CODE_DIRS $CODE_FILES) | tar -xf - -C "$DEST"
if [ ! -f "$DEST/config.yaml" ]; then
  cp "$SRC/config.yaml" "$DEST/config.yaml"
elif ! diff -q "$SRC/config.yaml" "$DEST/config.yaml" >/dev/null; then
  echo "NOTE: $DEST/config.yaml differs from the repo's config.yaml (kept as is)."
fi
chown -R signal:signal "$DEST"
chmod 600 "$DEST/.env"

# Python 3.12 from the system; no downloads.
(cd "$DEST" && sudo -u signal env HOME=/var/lib/signal UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev)

# Narrow sudo rules for /screenshot and /restart_mt5 (validated before it is installed).
visudo -cf "$SRC/deploy/sudoers/signal-mt5" >/dev/null
install -m 440 "$SRC/deploy/sudoers/signal-mt5" /etc/sudoers.d/signal-mt5

cp "$SRC/deploy/signal-service.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable signal-service >/dev/null
systemctl restart signal-service
systemctl is-active signal-service
for _ in $(seq 1 30); do
  curl -fsS http://127.0.0.1:8000/health && echo && exit 0
  sleep 1
done
echo "Service did not answer on /health: journalctl -u signal-service -n 50"
exit 1
