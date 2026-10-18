import json, re, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
AUDIT = ROOT / "results" / "audit.jsonl"
COMPOSE = str(ROOT / "docker-compose.yml")
CORE = {"web", "postgres", "redis"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
DATA_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")


def _name(p, core_only=False):
    n = str(p["name"])
    if not NAME_RE.fullmatch(n):
        raise ValueError("invalid container name")
    if core_only and n not in CORE:
        raise ValueError(f"name must be one of {sorted(CORE)}")
    return n


def _restart(p):
    return [["docker", "restart", _name(p, True)]]


def _start(p):
    return [["docker", "start", _name(p, True)]]


def _connect(p):
    return [["docker", "network", "connect", "sre_default", _name(p, True)]]


def _memory(p):
    n, mb = _name(p, True), int(p["mb"])
    if not 64 <= mb <= 2048:
        raise ValueError("mb must be 64-2048")
    return [["docker", "update", "--memory", f"{mb}m", "--memory-swap", f"{mb}m", n]]


def _remove(p):
    n = _name(p)
    if n in CORE:
        raise ValueError("refusing to remove a core service")
    return [["docker", "rm", "-f", n]]


def _delete(p):
    path = str(p["path"])
    if not path.startswith("/data/") or not DATA_RE.fullmatch(path[len("/data/"):]):
        raise ValueError("path must be a plain file name under /data/")
    return [["docker", "exec", "web", "rm", "-f", path]]


def _index(p):
    sql = "CREATE INDEX IF NOT EXISTS idx_events_user_id ON events(user_id)"
    return [["docker", "exec", "postgres", "psql", "-U", "app", "-d", "app", "-c", sql]]


def _config(p):
    good, live = ROOT / "config" / "app.json.good", ROOT / "config" / "app.json"
    return [["cp", str(good), str(live)], ["docker", "restart", "web"]]


def _web_defaults(p):
    return [["env", "-u", "DB_HOST", "-u", "DB_PASSWORD", "-u", "APP_VERSION",
             "docker", "compose", "-f", COMPOSE, "up", "-d", "--force-recreate", "--no-deps", "web"]]


def _db_sessions(p):
    base = ["docker", "exec", "postgres", "psql", "-U", "app", "-d", "app", "-c"]
    kill = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE usename = 'webapp'"
    return [base + ["ALTER ROLE webapp CONNECTION LIMIT -1"], base + [kill]]


ACTIONS = {
    "restart_container": ("Restart a core container (web, postgres or redis)", _restart),
    "start_container": ("Start a stopped core container", _start),
    "connect_network": ("Reconnect a core container to the app network", _connect),
    "set_memory_limit": ("Set a core container's memory limit in MB (64-2048)", _memory),
    "remove_container": ("Remove a non-core container, for example one holding a needed port", _remove),
    "delete_data_file": ("Delete one plain file under /data in the web container", _delete),
    "create_events_index": ("Recreate the user_id index on the events table", _index),
    "restore_config": ("Restore config/app.json from the known-good copy and restart web", _config),
    "recreate_web_defaults": ("Recreate web with the default environment (DB_HOST, DB_PASSWORD, APP_VERSION)", _web_defaults),
    "release_db_connections": ("End webapp's database sessions and remove its connection limit", _db_sessions),
}


def plan(action, **params):
    if action not in ACTIONS:
        raise ValueError("unknown action; available: " + ", ".join(sorted(ACTIONS)))
    desc, build = ACTIONS[action]
    try:
        cmds = build(params)
    except KeyError as e:
        raise ValueError(f"missing parameter {e}") from e
    return {"action": action, "params": params, "description": desc, "commands": cmds}


def _audit(entry):
    AUDIT.parent.mkdir(exist_ok=True)
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with AUDIT.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def execute(p, approved_by):
    out = []
    for c in p["commands"]:
        r = subprocess.run(c, capture_output=True, text=True, timeout=90)
        out.append({"cmd": c, "rc": r.returncode, "out": (r.stdout + r.stderr)[-500:]})
        if r.returncode:
            break
    _audit({"action": p["action"], "params": p["params"], "approved_by": approved_by, "result": out})
    return out


def main(argv):
    if not argv:
        sys.exit("usage: python actions.py <action> key=value ...\nactions: " + ", ".join(sorted(ACTIONS)))
    p = plan(argv[0], **dict(a.split("=", 1) for a in argv[1:]))
    print("PROPOSED:", p["description"])
    for c in p["commands"]:
        print("  $", " ".join(c))
    if input("Approve? [y/N] ").strip().lower() != "y":
        _audit({"action": p["action"], "params": p["params"], "approved_by": None, "result": "rejected"})
        print("rejected, nothing ran")
        return
    for o in execute(p, approved_by="cli-user"):
        print("rc", o["rc"], o["out"])


if __name__ == "__main__":
    main(sys.argv[1:])
