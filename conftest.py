import pytest
import subprocess as _real_subprocess


class _FakeRunResult:
    # what a docker call looks like when docker is unreachable
    returncode = 1
    stdout = ""
    stderr = "docker unavailable (test fake)"


@pytest.fixture(autouse=True)
def no_real_subprocess(monkeypatch):
    """No test may run a real command (docker, psql, cp). Tests that need a
    specific result patch subprocess.run themselves and override this."""
    calls = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        return _FakeRunResult()

    monkeypatch.setattr(_real_subprocess, "run", fake_run)
    return calls