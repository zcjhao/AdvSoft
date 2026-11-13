#!/bin/bash
cd /app; mkdir -p data static
(while true; do python3 /app/server.py irc 6667; sleep .5; done) &
for i in 0 1 2; do (while true; do python3 /app/server.py node $i 800$i; sleep .5; done) & done
wait
