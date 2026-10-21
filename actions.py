import json, re, subprocess, sys, time
from redact import redact
from pathlib import Path
import hashlib

ROOT = Path(__file__).resolve().parent
AUDIT = ROOT / "results" / "audit.jsonl"
COMPOSE = str(ROOT / "docker-compose.yml")
CORE = {"web", "postgres", "redis"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
DATA_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]*")

RESULTS = ROOT / "results"
PENDING_DIR = RESULTS / "pending"


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
    # Read docker-compose.yml at runtime with yaml.safe_load (no bare except).
    try:
        import yaml as _yaml
        compose_path = str(ROOT / "docker-compose.yml")
        with open(compose_path) as _f:
            compose = _yaml.safe_load(_f) or {}
        # Collect published host ports from the compose file.
        compose_ports = set()
        for _svc_name, _svc in compose.get("services", {}).items():
            for _pm in _svc.get("ports", []):
                if ":" in _pm:
                    _hp = _pm.split(":")[0]
                    if _hp.startswith("127.0.0.1:"):
                        _hp = _hp.split(":")[1]
                    if _hp.isdigit():
                        compose_ports.add(int(_hp))
        # Inspect the container's labels and published ports.
        import subprocess as sp, json as _json
        lbl_r = sp.run(
            ["docker", "inspect", "--format", "{{json .Labels}}", n],
            capture_output=True, text=True, timeout=10,
        )
        has_compose_label = False
        if lbl_r.returncode == 0 and lbl_r.stdout.strip():
            try:
                _labels = _json.loads(lbl_r.stdout)
                if _labels.get("com.docker.compose.service") or _labels.get(
                    "com.docker.compose.config-hash",
                ):
                    has_compose_label = True
            except Exception:
                pass
        pub_r = sp.run(
            ["docker", "inspect", "--format",
             "{{range .NetworkSettings.Ports}}{{index . 8000/tcp}}{{.HostPort}}{{end}}",
             n],
            capture_output=True, text=True, timeout=10,
        )
        publishes_needed_port = False
        if pub_r.returncode == 0 and pub_r.stdout.strip():
            _published = {int(p) for p in pub_r.stdout.split() if p.isdigit()}
            if _published & compose_ports:
                publishes_needed_port = True
        # Fail closed: refuse if core, has compose label, or publishes needed port.
        if has_compose_label or publishes_needed_port:
            raise ValueError(
                f"refusing to remove container with compose project association"
            )
    except Exception:
        # Any error during compose/inspection reading fails closed.
        raise ValueError("refusing to remove container: safety check failed")
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


def _name_image(p):
    """Get the Docker image for a container name, for safety checks."""
    try:
        import subprocess as sp
        r = sp.run(["docker", "inspect", "--format", "{{.Config.Image}}", _name(p, True)],
                     capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return None


# ACTIONS dict remains the same
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


def _canonical_json(data):
    """Produce a deterministic JSON string for hash comparison."""
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def plan(action, **params):
    if action not in ACTIONS:
        raise ValueError("unknown action; available: " + ", ".join(sorted(ACTIONS)))
    desc, build = ACTIONS[action]
    try:
        cmds = build(params)
    except KeyError as e:
        raise ValueError(f"missing parameter {e}") from e
    # Redact any output that might contain secrets before returning
    # (redaction happens at storage, not here)
    return {"action": action, "params": params, "description": desc, "commands": cmds}


def _ensure_pending_dir():
    PENDING_DIR.mkdir(parents=True, exist_ok=True)


def _write_pending(proposal_id, record):
    _ensure_pending_dir()
    with open(PENDING_DIR / f"{proposal_id}.json", "w") as f:
        json.dump(record, f, sort_keys=True)


def _read_pending(proposal_id):
    path = PENDING_DIR / f"{proposal_id}.json"
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def _delete_pending(proposal_id):
    path = PENDING_DIR / f"{proposal_id}.json"
    if path.exists():
        path.unlink()


def _container_info(p):
    """Get container image and published ports for inclusion in proposals."""
    try:
        import subprocess as sp, json
        n = _name(p)
        r = sp.run(["docker", "inspect", n], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            data = json.loads(r.stdout.strip())
            if data:
                c = data[0]
                image = c.get("Config", {}).get("Image", "unknown")
                # Published ports: docker inspect HostConfig.Ports
                ports = c.get("HostConfig", {}).get("Ports", [])
                # Simplify port info: show host port mappings
                port_strs = []
                for p_entry in (ports if isinstance(ports, list) else []):
                    if isinstance(p_entry, str):
                        # Format: "hostport:containerport proto" or "hostport->containerport/tcp"
                        if ":" in p_entry or "->" in p_entry:
                            port_strs.append(p_entry.split(":")[-1].split("->")[0].split("/")[0])
                return image, port_strs if port_strs else ["no published ports"]
    except Exception:
        pass
    return "unknown", ["unknown"]


def propose(action, **params):
    """Call plan(), store a pending proposal, and return the proposal id.

    Caps one pending proposal per investigation. If there is already a
    pending proposal for the same investigation, this one replaces it.
    The pending record includes the container's image and published ports
    for display in the approval UI.
    """
    p = plan(action, **params)
    # Build the canonical hash of the commands (never trust stored commands)
    cmd_hash = _canonical_json(p["commands"])
    now = time.time()

    # Check for existing pending proposal for the same investigation
    # The investigation id is derived from the action + params hash
    # If _run_id is present (bound via propose_action partial), include it so
    # the one-pending-per-run cap counts records per agent run
    run_id = params.pop("_run_id", None)
    if run_id:
        investigation_id = hashlib.sha256(f"{action}:{str(sorted(params.items()))}:{run_id}".encode()).hexdigest()[:16]
    else:
        investigation_id = hashlib.sha256(f"{action}:{str(sorted(params.items()))}".encode()).hexdigest()[:16]

    # Generate a proposal id for this pending record
    proposal_id = f"prop_{int(time.time()) % 1000000:06d}"

    existing = None
    for fname in PENDING_DIR.glob("*.json"):
        rec = _read_pending(fname.stem)
        if rec and rec.get("investigation_id") == investigation_id and rec["status"] == "pending":
            existing = rec
            break

    if existing and existing["id"] != p.get("_temp_id", ""):
        # There's already a pending proposal for this investigation; reject the old one
        if existing["id"] != p.get("_temp_id", ""):
            existing["status"] = "expired"
            _write_pending(existing["id"], existing)

    # Include container image and published ports in the pending record
    # (for display in the approval CLI and audit)
    image, ports = _container_info(params)

    # Create the pending record
    record = {
        "id": proposal_id if proposal_id else f"prop_{int(time.time()) % 1000000:06d}",
        "action": p["action"],
        "params": p["params"],
        "commands": p["commands"],  # stored but never trusted
        "cmd_hash": cmd_hash,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "status": "pending",
        "investigation_id": investigation_id,
        "image": image,
        "ports": ports,
    }
    _write_pending(record["id"], record)
    return record["id"]


def _is_stale(created_at_str):
    """Check if a proposal is older than 15 minutes."""
    from datetime import datetime, timedelta
    created = datetime.fromisoformat(created_at_str)
    now = datetime.now()
    return now - created > timedelta(minutes=15)


def approve(id, approved_by):
    """Rebuild commands from stored action/params, verify hash, run if valid.

    Returns True if executed, False if rejected.
    Gating: hash, staleness (15 min), and status checks all pass before
    the private _run_commands() is invoked. A caller-supplied approved_by
    string must never be sufficient on its own.
    """
    if not approved_by:
        raise ValueError("approve() requires a valid approval record (approved_by must not be empty)")

    record = _read_pending(id)
    if record is None:
        raise ValueError(f"no pending proposal with id {id}")

    # Check status
    if record["status"] != "pending":
        raise ValueError(f"proposal {id} is not pending (status: {record['status']})")

    # Check staleness (>15 min)
    if _is_stale(record["created_at"]):
        record["status"] = "expired"
        _write_pending(id, record)
        raise ValueError(f"proposal {id} is older than 15 minutes")

    # Rebuild commands from action + params via plan() (never trust stored commands)
    p = plan(record["action"], **record["params"])
    rebuilt_hash = _canonical_json(p["commands"])

    # Compare hash - if mismatch, reject
    if record["cmd_hash"] != rebuilt_hash:
        record["status"] = "rejected"
        _write_pending(id, record)
        raise ValueError(f"hash mismatch for proposal {id}: stored vs rebuilt commands differ")

    # Run the commands via the private runner (only callable from here)
    out, error = _run_commands(p["commands"])

    # Post-action verification: run read-only checks on the affected container
    # and store before/after state in the audit line.
    # We check the container's basic status before and after the action; if the
    # action was meant to fix a problem and the container still looks unhealthy,
    # we plainly note that instead of reporting success.
    import subprocess as sp, json

    # --- before status ---
    before_status = "unknown"
    container_name = None
    # Try to find the container name from params (common case: remove_container)
    if "name" in record["params"]:
        container_name = record["params"]["name"]
    elif record["action"] == "remove_container" and "name" in record["params"]:
        container_name = record["params"]["name"]

    if container_name:
        try:
            r = sp.run(["docker", "inspect", container_name],
                       capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout.strip())
                if data:
                    st = data[0].get("State", {})
                    if st.get("Running") is True:
                        before_status = "running"
                    elif st.get("OOMKilled"):
                        before_status = "oom_killed"
                    elif st.get("ExitCode") is not None:
                        before_status = f"stopped(exit={st.get('ExitCode')})"
                    else:
                        before_status = "unknown"
        except Exception:
            before_status = "inspect_failed"
    else:
        # For actions without a specific container name, note it
        before_status = "no_container_specified"

    # Run the commands via the private runner (only callable from here)
    out, error = _run_commands(p["commands"])

    # --- after status ---
    after_status = "unknown"
    if container_name:
        try:
            r = sp.run(["docker", "inspect", container_name],
                       capture_output=True, text=True, timeout=10)
            if r.returncode == 0 and r.stdout.strip():
                data = json.loads(r.stdout.strip())
                if data:
                    st = data[0].get("State", {})
                    if st.get("Running") is True:
                        after_status = "running"
                    elif st.get("OOMKilled"):
                        after_status = "oom_killed"
                    elif st.get("ExitCode") is not None:
                        after_status = f"stopped(exit={st.get('ExitCode')})"
                    else:
                        after_status = "unknown"
        except Exception:
            after_status = "inspect_failed"
    else:
        # For actions without a specific container name, note it
        after_status = "no_container_specified"

    # Determine container health verification state.
    # Values: "healthy" | "unhealthy" | "unverified" | "n/a"
    if after_status == "no_container_specified":
        verification = "n/a"
        still_unhealthy = False
    elif after_status in ("running",):
        verification = "healthy"
        still_unhealthy = False
    elif after_status in ("inspect_failed", "unknown"):
        verification = "unverified"
        still_unhealthy = False
    else:
        # oom_killed, stopped(exit=X), or other abnormal states
        verification = "unhealthy"
        still_unhealthy = True

    # Store audit line with hash, approver, and pre/post-action status.
    # Verification field tracks container health state: "healthy" | "unhealthy" | "unverified" | "n/a"
    # still_unhealthy is True only for "unhealthy" state.
    redacted_out = []
    for entry in out:
        redacted_out.append({
            "cmd": entry["cmd"],
            "rc": entry["rc"],
            "out": redact(entry["out"]) if isinstance(entry.get("out"), str) else entry.get("out", ""),
        })
    audit_record = {
        "action": record["action"],
        "params": record["params"],
        "approved_by": approved_by,
        "result": redacted_out,
        "error": error,
        "proposal_id": id,
        "cmd_hash": record["cmd_hash"],
        "before_status": before_status,
        "after_status": after_status,
        "still_unhealthy": still_unhealthy,
        "verification": verification,
    }
    _audit(audit_record)

    # Mark as executed (single-use)
    record["status"] = "executed"
    _write_pending(id, record)
    return True


def reject(id):
    """Mark a proposal as rejected."""
    record = _read_pending(id)
    if record is None:
        raise ValueError(f"no pending proposal with id {id}")
    record["status"] = "rejected"
    _write_pending(id, record)
    return False


def _audit(entry):
    AUDIT.parent.mkdir(exist_ok=True)
    entry["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    with AUDIT.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def _run_commands(commands):
    """Private: run a list of docker commands and return results.

    This runner is ONLY callable from approve() after all gating checks pass.
    It is never exposed to the model-visible tool schema.
    """
    out = []
    error = None
    try:
        for c in commands:
            r = subprocess.run(c, capture_output=True, text=True, timeout=90)
            out.append({"cmd": c, "rc": r.returncode, "out": (r.stdout + r.stderr)[-500:]})
            if r.returncode:
                break
    except subprocess.TimeoutExpired:
        error = "timeout"
        out.append({"cmd": commands[len(out)], "rc": -1, "out": "command timed out"})
    except FileNotFoundError:
        error = "missing binary"
    except Exception as e:
        error = f"{type(e).__name__}: {str(e)}"
    return out, error


def main(argv):
    if not argv:
        sys.exit("usage: python actions.py <action> key=value ...\nactions: " + ", ".join(sorted(ACTIONS)))
    p = plan(argv[0], **dict(a.split("=", 1) for a in argv[1:]))
    print("PROPOSED:", p["description"])
    for c in p["commands"]:
        print("  $", " ".join(c))
    # No inline approval anymore — the agent/program must use propose()/approve()
    # If called directly from CLI, we just show the proposal and exit
    print("(use propose/action flow outside this CLI)")
    return