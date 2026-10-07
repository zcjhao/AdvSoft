#!/bin/sh
set -eu
mkdir -p /app/data
python3 /app/server.py >/app/irc.log 2>&1 & echo $! >/app/irc.pid
pids=""
trap 'for p in $pids; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; exit 0' TERM INT
while :; do
  for n in 0 1 2; do
    port=$((8000+n)); file="/app/node${n}.pid"
    if [ ! -f "$file" ] || ! kill -0 "$(cat "$file" 2>/dev/null)" 2>/dev/null; then
      python3 /app/server.py "$port" >>/app/node${n}.log 2>&1 &
      pid=$!; echo "$pid" >"$file"; pids="$pids $pid"
    fi
  done
  sleep 1
done
