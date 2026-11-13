#!/bin/bash
cd /app
mkdir -p /app/data
( while true; do redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no; sleep 0.5; done ) &
for i in 0 1 2; do port=$((8000+i)); ( while true; do python3 /app/server.py node "$i" "$port"; sleep 0.5; done ) & done
( while true; do python3 /app/server.py irc 6667; sleep 0.5; done ) &
wait
