#!/usr/bin/env bash
# Usage: ./loadgen.sh [seconds]

END=$((SECONDS + ${1:-60}))

while [ "$SECONDS" -lt "$END" ]; do
    for p in \
        /health \
        /items \
        "/search?user_id=$((RANDOM % 10000))" \
        /cache \
        /write
    do
        curl -s -o /dev/null -m 5 \
            -w "$p %{http_code} %{time_total}s\n" \
            "http://localhost:8000$p" &
    done

    wait
    sleep 0.5
done
