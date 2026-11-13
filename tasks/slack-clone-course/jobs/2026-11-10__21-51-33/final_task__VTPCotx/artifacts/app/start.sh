#!/bin/bash
cd /app; mkdir -p data
for i in 0 1 2; do (while true; do python3 /app/server.py node $i $((8000+i)); sleep .5; done)& done
(while true; do python3 /app/server.py irc 6667; sleep .5; done)&
wait
