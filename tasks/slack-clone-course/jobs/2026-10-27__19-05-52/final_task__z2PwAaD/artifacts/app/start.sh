#!/bin/sh
mkdir -p /app/data
for n in 0 1 2; do
  (while true; do python3 /app/server.py "$n" "$((8000+n))" >>/tmp/huddle-$n.log 2>&1; sleep 1; done) &
done
wait
