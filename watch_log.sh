#!/bin/bash
# Watch Arena's log and report scene changes + new server messages live.
LOG="/c/Users/User/AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
START=$(wc -c < "$LOG")
echo "watching from byte $START at $(date +%H:%M:%S) -- go click Collection"
for i in $(seq 1 75); do
  sleep 2
  NOW=$(wc -c < "$LOG")
  if [ "$NOW" -gt "$START" ]; then
    CHUNK=$(tail -c +$((START+1)) "$LOG")
    echo "[$(date +%H:%M:%S)] +$((NOW-START)) bytes"
    echo "$CHUNK" | grep -o 'toSceneName":"[A-Za-z]*"' | sed 's/^/    SCENE -> /'
    echo "$CHUNK" | grep -o '<== [A-Za-z0-9_.]*' | sed 's/^/    MSG /' | sort -u
    START=$NOW
  fi
done
echo "watch ended at $(date +%H:%M:%S); final size $(wc -c < "$LOG")"
