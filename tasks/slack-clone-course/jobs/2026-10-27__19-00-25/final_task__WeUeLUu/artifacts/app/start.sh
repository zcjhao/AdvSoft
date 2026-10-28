#!/bin/sh
mkdir -p /app/data
while :; do
  for i in 0 1 2; do
    p=$((8000+i))
    if ! ss -ltn "sport = :$p" | grep -q LISTEN; then
      python3 /app/server.py "$i" "$p" >/tmp/huddle-$i.log 2>&1 &
    fi
  done
  sleep 1
done
