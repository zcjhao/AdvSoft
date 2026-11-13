#!/bin/bash
cd /app
mkdir -p /app/data
( while true; do redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no; sleep 0.5; done ) &
for i in 0 1 2; do
  ( while true; do python3 /app/server.py node "$i" "800$i"; sleep 0.5; done ) &
done
wait
