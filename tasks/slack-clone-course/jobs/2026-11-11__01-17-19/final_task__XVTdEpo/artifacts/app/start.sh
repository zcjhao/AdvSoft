#!/bin/sh
mkdir -p /app/data
while :; do
  if ! ss -ltn "sport = :6667" 2>/dev/null | grep -q LISTEN; then
    python3 /app/server.py irc 6667 >>/app/irc.log 2>&1 &
    echo $! >/app/irc.pid
  fi
  for n in 0 1 2; do
    p=$((8000+n))
    if ! ss -ltn "sport = :$p" 2>/dev/null | grep -q LISTEN; then
      python3 /app/server.py node "$n" "$p" >>/app/node$n.log 2>&1 &
      echo $! >/app/node$n.pid
    fi
  done
  sleep 1
done
