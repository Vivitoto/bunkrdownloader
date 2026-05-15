#!/bin/bash
set -euo pipefail

mkdir -p /data/downloads /data/logs /data/config

# Seed default settings on first run
if [ ! -f /data/config/settings.json ]; then
  cp /app/config.default.json /data/config/settings.json
fi

touch /data/config/URLs.txt

echo "[entry] starting BunkrDownloader WebUI on :8877"
exec python3 webui/app.py
