#!/bin/bash
cd /app; mkdir -p data files
for i in 0 1 2; do (while true; do /opt/venv/bin/python /app/server.py node $i 800$i; sleep .5; done) & done
wait
