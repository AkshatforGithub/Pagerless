import pytest
import tools


@pytest.mark.parametrize("name", sorted(tools.DIAGNOSTICS))
def test_diagnostic_returns_text_not_none(name, monkeypatch):
    monkeypatch.setattr(tools, "_run", lambda cmd, timeout=15: (0, "[]"))
    out = tools.run_diagnostic(name)
    assert isinstance(out, str)
    assert not out.startswith("ERROR: unknown")
