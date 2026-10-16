#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
F="${1:?usage: ./inject.sh <fault_id>}"
PSQL="docker exec -i postgres psql -U app -d app -q"
case "$F" in
  crash_exit)        curl -s -m 3 localhost:8000/admin/crash || true ;;
  db_stopped)        docker stop postgres >/dev/null ;;
  redis_stopped)     docker stop redis >/dev/null ;;
  wrong_db_password) DB_PASSWORD=wrongpass docker compose up -d --force-recreate --no-deps web ;;
  wrong_db_host)     DB_HOST=postgress docker compose up -d --force-recreate --no-deps web ;;
  oom_kill)
    docker update --memory 96m --memory-swap 96m web >/dev/null
    curl -s -m 5 "localhost:8000/admin/leak?mb=300" || true ;;
  memory_leak)       curl -s -m 3 localhost:8000/admin/leak_start ;;
  cpu_spin)          curl -s -m 3 "localhost:8000/admin/spin?seconds=3600" ;;
  missing_index)     $PSQL -c "DROP INDEX idx_events_user_id" ;;
  conn_exhausted)
    $PSQL -c "ALTER ROLE webapp CONNECTION LIMIT 3"
    for i in 1 2 3; do
      docker exec -d postgres psql -U webapp -d app -c "select pg_sleep(3600)"
    done ;;
  disk_full)         docker exec web dd if=/dev/zero of=/data/fill bs=1M count=60 2>/dev/null || true ;;
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
