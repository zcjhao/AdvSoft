#!/bin/sh
mkdir -p /app/data
run_node() { n="$1"; p="$2"; while :; do python3 /app/server.py node "$n" "$p" >>/app/node"$n".log 2>&1; sleep .5; done; }
run_redis() { while :; do python3 /app/redis_stub.py >>/app/redis.log 2>&1; sleep .5; done; }
run_irc() { while :; do python3 /app/server.py irc 6667 >>/app/irc.log 2>&1; sleep .5; done; }
run_node 0 8000 & run_node 1 8001 & run_node 2 8002 & run_redis & run_irc & wait
