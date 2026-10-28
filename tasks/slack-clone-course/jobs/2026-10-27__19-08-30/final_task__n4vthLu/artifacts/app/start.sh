#!/bin/sh
mkdir -p /app/data
for i in 0 1 2; do
  port=$((8000+i))
  while true; do python3 /app/server.py "$i" "$port" >>"/tmp/huddle-$i.log" 2>&1; sleep 1; done &
done
wait
