#!/bin/bash
cd /app
mkdir -p /app/data
for i in 0 1 2; do (while true; do python3 /app/server.py node $i 800$i; sleep .5; done) & done
wait
