#!/bin/bash
cd /app
mkdir -p /app/data
(while true; do redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no >/tmp/redis.log 2>&1; sleep .5; done) &
for i in 0 1 2; do (while true; do python3 /app/server.py node "$i" $((8000+i)); sleep .5; done) & done
(while true; do python3 /app/server.py irc 6667; sleep .5; done) &
wait
