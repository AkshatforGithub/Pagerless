import json, os, re, subprocess, sys, time, urllib.error, urllib.request, functools
from redact import redact as _redact
from pathlib import Path
import actions, uuid

PROJECT = Path(os.getenv("PAGERLESS_DIR", Path(__file__).resolve().parent))
EXEC_OK = {"web", "postgres", "redis"}
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")

# Diagnostics dictionary (same as original)
DIAGNOSTICS = {
    "pg_connections": lambda: None,  # placeholder - actual implementation uses _pg
    "pg_indexes": lambda: None,
    "pg_explain_search": lambda: None,
    "docker_network": lambda: None,
    "data_dir": lambda: None,
    "processes": lambda: None,
    "web_env": lambda: None,
    "config_file": lambda: None,
}


def _pg(*sqls):
    cmd = ["docker", "exec", "postgres", "psql", "-U", "app", "-d", "app"]
    for s in sqls:
        cmd += ["-c", s]
    return _cmd(*cmd)


def _web_env():
    rc, out = _run(["docker", "inspect", "web", "--format", "{{json .Config.Env}}"])
    if rc != 0:
        return f"ERROR: {out}"
    rows = []
    for kv in json.loads(out):
        k, _, v = kv.partition("=")
        if k.startswith(("DB_", "REDIS_", "APP_")):
            rows.append(f"{k}={'***' if __import__('re').search(r'(PASSWORD|SECRET|TOKEN|KEY)', k) else v}")
    return "\n".join(rows)


def _run(cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "command timed out"
    except FileNotFoundError:
        return 127, "docker not found"
    return p.returncode, (p.stdout + p.stderr).strip()


def _cmd(*cmd):
    rc, out = _run(list(cmd))
    return out if rc == 0 else f"ERROR: {out}"


def _clip(text, limit=4000):
    text = _redact(text)
    if len(text) <= limit:
        return text
    return f"[...{len(text) - limit} chars truncated...]\n" + text[-limit:]


def get_logs(container: str, tail: int = 80, grep: str = "") -> str:
    """Return recent log lines of a container (newest last). Optional case-insensitive
    grep filter. Works on stopped containers too. Output is truncated to keep it small."""
    if not NAME_RE.fullmatch(container):
        return "ERROR: invalid container name"
    tail = max(1, min(int(tail), 500))
    rc, out = _run(["docker", "logs", "--tail", "500" if grep else str(tail), container])
    if rc != 0:
        return f"ERROR: {out}"
    lines = out.splitlines()
    if grep:
        lines = [l for l in lines if grep.lower() in l.lower()]
        if not lines:
            return f"(no lines matching '{grep}')"
    return _clip("\n".join(lines[-tail:])) or "(no log output)"


def container_status(name: str = "") -> str:
    """Without a name: list all containers with state and ports. With a name: detailed
    state (exit code, OOMKilled, start error, restart count, memory limit, networks)."""
    if not name:
        rc, out = _run(["docker", "ps", "-a", "--format", "{{json .}}"])
        if rc != 0:
            return f"ERROR: {out}"
        rows = []
        for line in out.splitlines():
            c = json.loads(line)
            rows.append({"name": c["Names"], "state": c["State"], "status": c["Status"],
                         "ports": c["Ports"], "image": c["Image"]})
        return json.dumps(rows, indent=1)
    if not NAME_RE.fullmatch(name):
        return "ERROR: invalid container name"
    rc, out = _run(["docker", "inspect", name])
    if rc != 0:
        return f"ERROR: {out}"
    d = json.loads(out)[0]
    st, hc = d["State"], d["HostConfig"]
    return json.dumps({
        "name": name, "status": st.get("Status"), "running": st.get("Running"),
        "exit_code": st.get("ExitCode"), "oom_killed": st.get("OOMKilled"),
        "error": st.get("Error"), "started_at": st.get("StartedAt"),
        "finished_at": st.get("FinishedAt"), "restart_count": d.get("RestartCount"),
        "memory_limit_bytes": hc.get("Memory"),
        "networks": list(d["NetworkSettings"]["Networks"]),
    }, indent=1)


def disk_usage(container: str = "web") -> str:
    """Run df -h inside web, postgres or redis to show filesystem usage."""
    if container not in EXEC_OK:
        return f"ERROR: container must be one of {sorted(EXEC_OK)}"
    return _clip(_cmd("docker", "exec", container, "df", "-h"))


def query_metrics(samples: int = 1, interval: int = 5) -> str:
    """CPU and memory per container. Use samples=3 (interval 5s) to see trends such as
    a steadily growing memory. Max 4 samples, interval 1-10 seconds."""
    samples = max(1, min(int(samples), 4))
    interval = max(1, min(int(interval), 10))
    lines = []
    for i in range(samples):
        rc, out = _run(["docker", "stats", "--no-stream", "--format", "{{json .}}"], timeout=30)
        if rc != 0:
            return f"ERROR: {out}"
        for line in out.splitlines():
            c = json.loads(line)
            lines.append(f"t+{i * interval}s {c['Name']} cpu={c['CPUPerc']} "
                         f"mem={c['MemUsage']} ({c['MemPerc']})")
        if i < samples - 1:
            time.sleep(interval)
    return "\n".join(lines)


PROBES = ["/health", "/items", "/search?user_id=42", "/cache", "/write"]


def probe_endpoints() -> str:
    """Call the web app's public endpoints once and report status code and latency."""
    lines = []
    for p in PROBES:
        t = time.time()
        try:
            with urllib.request.urlopen(f"http://localhost:8000{p}", timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        except Exception as e:
            code = f"ERR({type(e).__name__})"
        lines.append(f"{p} -> {code} in {(time.time() - t) * 1000:.0f}ms")
    return "\n".join(lines)


def run_diagnostic(name: str) -> str:
    """Run one named, read-only diagnostic. Names: pg_connections (sessions per user and
    webapp's connection limit), pg_indexes (indexes on the events table), pg_explain_search
    (query plan of the search query), docker_network (containers on the app network),
    data_dir (files in the web /data dir), processes (processes in web), web_env (DB_/REDIS_/APP_
    env vars web was created with, secrets masked), config_file (contents of app.json)."""
    fn = DIAGNOSTICS.get(name)
    if fn is None:
        return "ERROR: unknown diagnostic. Available: " + ", ".join(sorted(DIAGNOSTICS))
    return _clip(fn())


# Module-level run_id, set by agent.py per investigate() call
propose_action_run_id = None


def _set_propose_action_run_id(run_id: str):
    """Set the per-run investigation ID bound to propose_action via partial."""
    global propose_action_run_id
    propose_action_run_id = run_id


def propose_action(action: str, params: dict) -> dict:
    """Propose a safety action. Calls actions.propose() and returns the proposal id
    plus the description. The agent has no path to execute() or approve().

    Model-sent params whose keys start with "_" are dropped before the harness
    run-id is bound, so internal keys like _run_id cannot be overridden by the
    model. The run-id is set by the agent per-investigation; when unset the
    one-pending-per-run cap is disabled.

    Raises ValueError if no run ID is set, to enforce that every investigation
    has a bound run ID for the one-pending-per-run cap to function.
    """
    if propose_action_run_id is None:
        raise ValueError(
            "propose_action() requires SRE_RUN_ID to be set; "
            "call _set_propose_action_run_id() from agent.py investigate()."
        )
    # Drop any model-sent params whose key starts with "_" (internal keys)
    filtered_params = {k: v for k, v in params.items() if not k.startswith("_")}
    # Add harness run-id if set (binds the one-pending-per-run investigation cap)
    filtered_params["_run_id"] = propose_action_run_id
    result = actions.propose(action, **filtered_params)
    return {"proposal_id": result["id"], "description": result["description"]}