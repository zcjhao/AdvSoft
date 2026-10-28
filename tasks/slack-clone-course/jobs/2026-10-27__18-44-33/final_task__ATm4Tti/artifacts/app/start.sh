#!/bin/sh
mkdir -p /app/data
run_node() {
  n="$1"; p="$2"
  while :; do
    python3 /app/server.py "$n" "$p" >>"/tmp/huddle-$n.log" 2>&1
    sleep 1
  done
}
run_node 0 8000 & a=$!
run_node 1 8001 & b=$!
run_node 2 8002 & c=$!
trap 'kill "$a" "$b" "$c" 2>/dev/null; wait' INT TERM EXIT
wait
