#!/bin/bash
# auto-restart loop for the paper daemon + static dashboard server (localhost only)
cd "$(dirname "$0")"
PORT=$(python3 -c "import json;print(json.load(open('config.json'))['dashboard_port'])")
(while true; do nice -n 10 python3 tt.py snapshot; sleep 60; done) &
(cd web && while true; do nice -n 10 python3 -m http.server $PORT --bind 127.0.0.1 >/dev/null 2>&1; sleep 5; done) &
while true; do nice -n 10 python3 tt.py run >> daemon.log 2>&1; echo "restart $(date)" >> daemon.log; sleep 10; done
