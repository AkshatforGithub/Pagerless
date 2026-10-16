import json
import logging
import os
import threading
import time
import uuid

import psycopg
import redis

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

log = logging.getLogger("web")

VERSION = os.getenv("APP_VERSION", "v1")

with open("/config/app.json") as f:
    CONFIG = json.load(f)

DB = {
    "host": os.getenv("DB_HOST", "postgres"),
    "user": "webapp",
    "password": os.getenv("DB_PASSWORD", "app"),
    "dbname": "app",
    "connect_timeout": 3,
}

R = redis.Redis(
    host=os.getenv("REDIS_HOST", "redis"),
    socket_connect_timeout=2,
    socket_timeout=2,
    decode_responses=True,
)

LEAK = []

app = FastAPI()

log.info("web starting version=%s", VERSION)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()

    try:
        response = await call_next(request)
    except Exception:
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse(
            {"error": "internal"},
            status_code=500,
        )

    if not request.url.path.startswith("/admin"):
        log.info(
            "%s %s -> %s %dms",
            request.method,
            request.url.path,
            response.status_code,
            (time.time() - start) * 1000,
        )

    return response


def q(sql, args=()):
    with psycopg.connect(**DB) as conn:
        return conn.execute(sql, args).fetchall()


@app.get("/health")
def health():
    try:
        q("SELECT 1")
        R.ping()
    except Exception:
        log.exception("health check failed")
        raise HTTPException(status_code=503, detail="unhealthy")

    return {"status": "ok", "version": VERSION}


@app.get("/items")
def items():
    rows = [
        {"id": row[0], "name": row[1]}
        for row in q("SELECT id, name FROM items LIMIT 5")
    ]

    # Deliberate regression for SRE fault diagnosis.
    if VERSION == "v2-bad":
        rows.append({
            "total": sum(row["price"] for row in rows)
        })

    return rows


@app.get("/search")
def search(user_id: int = 1):
    start = time.time()

    count = q(
        "SELECT count(*) FROM events WHERE user_id = %s",
        (user_id,),
    )[0][0]

    elapsed_ms = (time.time() - start) * 1000

    if elapsed_ms > 100:
        log.warning(
            "slow query: events lookup took %.2fms",
            elapsed_ms,
        )

    return {"user_id": user_id, "events": count}


@app.get("/cache")
def cache():
    return {"hits": R.incr("hits")}


@app.get("/write")
def write():
    path = f"/data/tmp_{uuid.uuid4().hex}"

    try:
        with open(path, "wb") as f:
            f.write(b"x" * 50000)
    except OSError:
        log.exception("write failed")
        raise HTTPException(status_code=500, detail="write failed")
    finally:
        if os.path.exists(path):
            os.remove(path)

    return {"ok": True}


# Fault-injection endpoints.
# Use only in your local test environment.


@app.get("/admin/crash")
def crash():
    log.critical("fatal error in worker, exiting")
    os._exit(1)


@app.get("/admin/leak")
def leak(mb: int = 100):
    if mb < 1 or mb > 100:
        raise HTTPException(
            status_code=400,
            detail="mb must be between 1 and 100",
        )

    LEAK.append(b"x" * (mb * 1024 * 1024))

    return {"leaked_mb": mb}


def _leaker():
    for _ in range(80):
        LEAK.append(b"x" * (5 * 1024 * 1024))
        time.sleep(1)


@app.get("/admin/leak_start")
def leak_start():
    threading.Thread(target=_leaker, daemon=True).start()
    return {"ok": True}


def _spin(seconds):
    end = time.time() + seconds

    while time.time() < end:
        pass


@app.get("/admin/spin")
def spin(seconds: int = 60):
    if seconds < 1 or seconds > 60:
        raise HTTPException(
            status_code=400,
            detail="seconds must be between 1 and 60",
        )

    threading.Thread(
        target=_spin,
        args=(seconds,),
        daemon=True,
    ).start()

    return {"ok": True}
