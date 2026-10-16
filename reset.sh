#!/usr/bin/env bash
cd "$(dirname "$0")"
PSQL="docker exec -i postgres psql -U app -d app -q"
docker rm -f squatter >/dev/null 2>&1 || true
cp config/app.json.good config/app.json
docker network connect sre_default postgres 2>/dev/null || true
docker compose up -d postgres redis >/dev/null
until docker exec postgres pg_isready -U app -q; do sleep 1; done
$PSQL -c "ALTER ROLE webapp CONNECTION LIMIT -1"
$PSQL -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename='webapp'" >/dev/null
$PSQL -c "CREATE INDEX IF NOT EXISTS idx_events_user_id ON events(user_id)"
docker compose up -d --force-recreate --no-deps web >/dev/null
for i in $(seq 1 30); do
  curl -sf localhost:8000/health >/dev/null && echo "stack healthy" && exit 0
  sleep 1
done
echo "stack NOT healthy"; exit 1
