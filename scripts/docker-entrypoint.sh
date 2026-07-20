#!/bin/bash
set -euo pipefail

python3 scripts/start-local.py --services-only &
monitor_pid=$!

npm run start -- --port 3002 --hostname 127.0.0.1 &
web_pid=$!

nginx -g 'daemon off;' &
proxy_pid=$!

shutdown() {
  kill "$monitor_pid" "$web_pid" "$proxy_pid" 2>/dev/null || true
  wait "$monitor_pid" "$web_pid" "$proxy_pid" 2>/dev/null || true
}

trap shutdown EXIT INT TERM

while kill -0 "$monitor_pid" 2>/dev/null \
  && kill -0 "$web_pid" 2>/dev/null \
  && kill -0 "$proxy_pid" 2>/dev/null; do
  sleep 1
done

exit 1
