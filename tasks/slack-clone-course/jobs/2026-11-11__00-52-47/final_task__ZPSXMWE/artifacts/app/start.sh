#!/bin/sh
mkdir -p /app/data
run_redis() {
  while :; do
    redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/app/redis.log 2>&1
    sleep .5
  done
}
run_irc() {
  while :; do python3 /app/server.py irc 6667 >>/app/irc.log 2>&1; sleep .5; done
}
run_node() {
  while :; do
    python3 /app/server.py node "$1" "$2" >>/app/node-"$1".log 2>&1
    sleep .5
  done
}
run_redis &
run_irc &
run_node 0 8000 &
run_node 1 8001 &
run_node 2 8002 &
wait
