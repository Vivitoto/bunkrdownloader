#!/bin/bash
set -euo pipefail

# Ensure data directories exist (volume mounts may override these)
for d in /data/downloads /data/logs /data/config; do
  mkdir -p "$d" 2>/dev/null || true
done

# Seed default settings only if the file is missing; ignore permission errors
# (webui/app.py already falls back to DEFAULT_SETTINGS when nothing is readable)
if [ ! -f /data/config/settings.json ]; then
  cp /app/config.default.json /data/config/settings.json 2>/dev/null || true
fi

# Seed URLs.txt
touch /data/config/URLs.txt 2>/dev/null || true

echo "[entry] starting BunkrDownloader WebUI on :8877"
exec python3 webui/app.py
