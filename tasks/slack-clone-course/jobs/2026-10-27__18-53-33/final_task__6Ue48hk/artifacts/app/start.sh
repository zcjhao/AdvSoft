#!/bin/sh
mkdir -p /app/data
for i in 0 1 2; do
  port=$((8000+i))
  (while :; do python3 /app/server.py "$i" "$port"; sleep 1; done) &
done
wait
