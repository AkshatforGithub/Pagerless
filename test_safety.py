import json
import pytest
import actions, tools
from redact import redact

SECRETS = [
    ("DB_PASSWORD=hunter2 connecting", "hunter2"),
    ("password: hunter2", "hunter2"),
    ('{"password": "hunter2"}', "hunter2"),
    ("Authorization: Bearer abc.def-123", "abc.def-123"),
    ("postgres://app:s3cret@postgres:5432/app", "s3cret"),
    ("key is gsk_" + "a1B2" * 8, "a1B2" * 8),
]

NORMAL = [
    "2026-10-17 08:06:10,036 CRITICAL fatal error in worker, exiting",
    "slow query: events lookup took 450ms",
    'FATAL: password authentication failed for user "webapp"',
    'too many connections for role "webapp"',
    "web starting version=v1",
    "No space left on device",
]

BAD_NAMES = ["web; rm -rf /", "web\n", "-v", "../etc", "a" * 100, "web extra"]


@pytest.mark.parametrize("text,secret", SECRETS)
def test_secrets_are_redacted(text, secret):
    assert secret not in redact(text)
    assert secret not in tools._clip(text)


@pytest.mark.parametrize("line", NORMAL)
def test_normal_log_lines_unchanged(line):
    assert redact(line) == line


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_tools_reject_bad_names(bad):
    assert tools.get_logs(bad).startswith("ERROR")
    assert tools.container_status(bad).startswith("ERROR")
    assert tools.disk_usage(bad).startswith("ERROR")


@pytest.mark.parametrize("bad", ["rm -rf /", "pg_indexes; ls", "", "squatter"])
def test_unknown_diagnostics_rejected(bad):
    assert tools.run_diagnostic(bad).startswith("ERROR")


BAD_ACTIONS = [
    ("restart_container", {"name": "squatter"}),
    ("restart_container", {"name": "web; ls"}),
    ("restart_container", {}),
    ("remove_container", {"name": "web"}),
    ("remove_container", {"name": "postgres"}),
    ("delete_data_file", {"path": "/etc/passwd"}),
    ("delete_data_file", {"path": "/data/../etc/passwd"}),
    ("delete_data_file", {"path": "/data/.."}),
    ("set_memory_limit", {"name": "web", "mb": "1"}),
    ("set_memory_limit", {"name": "web", "mb": "999999"}),
    ("nonexistent", {}),
]


@pytest.mark.parametrize("action,params", BAD_ACTIONS)
def test_actions_reject_bad_input(action, params):
    with pytest.raises(ValueError):
        actions.plan(action, **params)


def test_plans_are_plain_argv_lists():
    p = actions.plan("restart_container", name="web")
    assert p["commands"] == [["docker", "restart", "web"]]
    for a, kw in [("remove_container", {"name": "squatter"}), ("delete_data_file", {"path": "/data/fill"}),
                  ("set_memory_limit", {"name": "web", "mb": "256"}), ("release_db_connections", {})]:
        for c in actions.plan(a, **kw)["commands"]:
            assert isinstance(c, list) and all(isinstance(x, str) for x in c)


def test_rejection_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(actions, "AUDIT", tmp_path / "audit.jsonl")
    calls = []
    monkeypatch.setattr(actions.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr("builtins.input", lambda _="": "n")
    actions.main(["restart_container", "name=web"])
    assert calls == []
    assert "rejected" in (tmp_path / "audit.jsonl").read_text()


def test_execute_writes_audit_record(tmp_path, monkeypatch):
    class R:
        returncode, stdout, stderr = 0, "ok", ""
    monkeypatch.setattr(actions, "AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(actions.subprocess, "run", lambda *a, **k: R())
    actions.execute(actions.plan("restart_container", name="web"), approved_by="tester")
    rec = json.loads((tmp_path / "audit.jsonl").read_text())
    assert rec["approved_by"] == "tester" and rec["action"] == "restart_container"
