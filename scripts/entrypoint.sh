#!/bin/bash
set -euo pipefail

# Ensure symlinks / config persist across restarts
mkdir -p /data/downloads /data/logs /data/config

# Copy default settings if not present
if [ ! -f /data/config/settings.json ]; then
    cp /app/config.default.json /data/config/settings.json 2>/dev/null || \
    echo '{"concurrency":3,"maxRetries":5,"downloadPath":"/data/downloads","ignoreList":"","includeList":""}' > /data/config/settings.json
fi

# Create URLs.txt if not present
touch /data/config/URLs.txt

echo "[entry] starting BunkrDownloader WebUI on :8877"
exec python3 webui/app.py
