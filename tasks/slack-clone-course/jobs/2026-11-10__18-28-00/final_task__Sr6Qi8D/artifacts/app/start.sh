#!/bin/sh
mkdir -p /app/data
while :; do redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no >/app/data/redis.log 2>&1; sleep .5; done &
for n in 0 1 2; do (while :; do python3 /app/server.py node "$n" "$((8000+n))" >/app/data/node$n.log 2>&1; sleep .5; done) & done
(while :; do python3 /app/server.py irc 6667 >/app/data/irc.log 2>&1; sleep .5; done) &
wait
