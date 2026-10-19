#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
F="${1:?usage: ./inject.sh <fault_id>}"
PSQL="docker exec -i postgres psql -u app -d app -q"
case "$F" in
  crash_exit)        curl -s -m 3 localhost:8000/admin/crash || true ;;
  db_stopped)        docker stop postgres >/dev/null ;;
  redis_stopped)     docker stop redis >/dev/null ;;
  wrong_db_password) DB_PASSWORD=wrongpass docker compose up -d --force-recreate --no-deps web ;;
  wrong_db_host)     DB_HOST=postgress docker compose up -d --force-recreate --no-deps web ;;
  oom_kill)
    docker update --memory 96m --memory-swap 96m web >/dev/null
    if ! response=$(curl -s -w "HTTPSTATUS:%{http_code}" -m 5 "localhost:8000/admin/leak?mb=300"); then
      echo "connection dropped (expected if the container was OOM-killed)"
      # Verify the container was actually OOM-killed
      if [ "$(docker inspect web --format '{{.State.OOMKilled}}')" != "true" ]; then
        echo "Error: Container was not OOM-killed despite connection drop"
        exit 1
      fi
    else
      http_status=${response#*HTTPSTATUS:}
      body=${response%HTTPSTATUS*}
      if [ "$http_status" -ge 400 ]; then
        echo "Error injecting oom_kill: $body"
        exit 1
      fi
    fi
    ;;
  memory_leak)
    response=$(curl -s -w "HTTPSTATUS:%{http_code}" -m 3 localhost:8000/admin/leak_start) || { echo "curl failed"; exit 1; }
    http_status=${response#*HTTPSTATUS:}
    body=${response%HTTPSTATUS*}
    if [ "$http_status" -ge 400 ]; then
      echo "Error injecting memory_leak: $body"
      exit 1
    fi
    ;;
  cpu_spin)
    response=$(curl -s -w "HTTPSTATUS:%{http_code}" -m 3 "localhost:8000/admin/spin?seconds=600") || { echo "curl failed"; exit 1; }
    http_status=${response#*HTTPSTATUS:}
    body=${response%HTTPSTATUS*}
    if [ "$http_status" -ge 400 ]; then
      echo "Error injecting cpu_spin: $body"
      exit 1
    fi
    ;;
  missing_index)     $PSQL -c "DROP INDEX idx_events_user_id" ;;
  conn_exhausted)
    $PSQL -c "ALTER ROLE webapp CONNECTION LIMIT 3"
    for i in 1 2 3; do
      docker exec -d postgres psql -u webapp -d app -c "select pg_sleep(3600)"
    done ;;
  disk_full)         docker exec web dd if=/dev/zero of=/data/fill bs=1M count=60 2>/dev/null ||  true ;;
  port_conflict)
    docker compose stop web
    docker run -d --name squatter -p 8000:8000 python:3.12-slim python -m http.server 8000 >/dev/null
    docker compose start web || true ;;
  bad_config)        echo '{broken' >> config/app.json; docker compose restart web ;;
  network_partition) docker network disconnect sre_default postgres ;;
  bad_deploy)        APP_VERSION=v2-bad docker compose up -d --force-recreate --no-deps web ;;
  *) echo "unknown fault: $F"; exit 1 ;;
esac
echo "injected: $F"