#!/bin/bash
cd /app; mkdir -p data
exec 9>/tmp/huddle.supervisor.lock
flock -n 9 || exit 0
(while true; do redis-server --port 6379 --bind 127.0.0.1 --save '' --appendonly no; sleep .5; done) &
for i in 0 1 2; do (while true; do sleep $((i+1)); python3 /app/server.py node $i 800$i; sleep .5; done) & done
(while true; do python3 /app/server.py irc 6667; sleep .5; done) &
wait
