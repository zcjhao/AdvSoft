#!/bin/sh
mkdir -p /app/data
run_node() {
  node="$1"
  port="$2"
  while :; do
    python3 /app/server.py node "$node" "$port" >"/tmp/node${node}.log" 2>&1
    sleep 0.5
  done
}
run_redis() {
  while :; do
    redis-server --bind 127.0.0.1 --port 6379 --save '' --appendonly no >/tmp/redis.log 2>&1
    sleep 0.5
  done
}
run_redis &
run_irc() { while :; do python3 /app/server.py irc 6667 >/tmp/irc.log 2>&1; sleep .5; done }
run_irc &
run_node 0 8000 &
run_node 1 8001 &
run_node 2 8002 &
wait
