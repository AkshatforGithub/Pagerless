import asyncio
import sys
import types

import pytest

import agent


class FakeObs:
    def __init__(self, rec, kind, name, kw):
        self.rec, self.kind, self.name, self.kw = rec, kind, name, kw
        self.updates = []

    def update(self, **kw):
        self.updates.append(kw)
        self.rec.events.append(("update", self.name))


class FakeCM:
    def __init__(self, rec, kind, name, kw):
        self.rec = rec
        self.obs = FakeObs(rec, kind, name, kw)

    def __enter__(self):
        self.rec.events.append(("enter", self.obs.name))
        self.rec.observations.append(self.obs)
        return self.obs

    def __exit__(self, *exc):
        self.rec.events.append(("exit", self.obs.name))
        return False


class FakeLangfuse:
    def __init__(self):
        self.events, self.observations, self.flushed = [], [], 0

    def start_as_current_observation(self, as_type="span", name="", **kw):
        return FakeCM(self, as_type, name, kw)

    def flush(self):
        self.flushed += 1
        self.events.append(("flush", None))


@pytest.fixture
def fake_langfuse(monkeypatch):
    lf = FakeLangfuse()
    mod = types.ModuleType("langfuse")
    mod.get_client = lambda: lf
    monkeypatch.setitem(sys.modules, "langfuse", mod)
    return lf


def test_flag_off_never_imports_langfuse(monkeypatch):
    monkeypatch.delitem(sys.modules, "langfuse", raising=False)
    tr = agent._Tracer(False, "investigation")
    with tr.span("tool_x") as obs:
        assert obs is None
    tr.close()
    assert "langfuse" not in sys.modules


def test_spans_generation_and_flush_order(fake_langfuse):
    tr = agent._Tracer(True, "investigation", input="alert")
    with tr.span("llm-call", as_type="generation", model="m") as gen:
        tr.update(gen, usage_details={"input_tokens": 3, "output_tokens": 2})
    with tr.span("tool_get_logs", input="{}") as t:
        tr.update(t, output="ok")
    tr.close(output={"tokens": 5})
    obs = {o.name: o for o in fake_langfuse.observations}
    assert obs["investigation"].kind == "span"
    assert obs["llm-call"].kind == "generation" and obs["llm-call"].kw["model"] == "m"
    assert obs["llm-call"].updates[0]["usage_details"] == {"input_tokens": 3, "output_tokens": 2}
    assert obs["tool_get_logs"].updates[0]["output"] == "ok"
    ev = fake_langfuse.events
    assert (ev.index(("enter", "investigation")) < ev.index(("enter", "llm-call"))
            < ev.index(("exit", "llm-call")) < ev.index(("exit", "investigation"))
            < ev.index(("flush", None)))


def test_langfuse_unusable_warns_and_continues(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "langfuse", None)  # makes the import fail
    tr = agent._Tracer(True, "investigation")
    with tr.span("tool_x") as obs:
        assert obs is None
    tr.close()
    assert "WARNING" in capsys.readouterr().err


def test_tracing_errors_never_break_the_run(fake_langfuse, monkeypatch):
    tr = agent._Tracer(True, "investigation")

    def bad_flush():
        raise RuntimeError("flush failed")

    monkeypatch.setattr(fake_langfuse, "flush", bad_flush)
    with tr.span("tool_x") as obs:
        def boom(**kw):
            raise RuntimeError("update failed")
        obs.update = boom
        tr.update(obs, output="y")  # must not raise
    tr.close()  # must not raise


def test_real_exception_propagates_and_span_closes(fake_langfuse):
    tr = agent._Tracer(True, "investigation")
    with pytest.raises(ValueError):
        with tr.span("tool_x"):
            raise ValueError("real error")
    tr.close()
    assert ("exit", "tool_x") in fake_langfuse.events


def test_investigate_closes_and_flushes_when_body_raises(fake_langfuse, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("model down")

    monkeypatch.setattr(agent, "_investigate", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(agent.investigate("m", langtrace="1"))
    assert ("exit", "investigation") in fake_langfuse.events
    assert ("flush", None) in fake_langfuse.events


def test_investigate_flag_from_env_and_final_stats(fake_langfuse, monkeypatch):
    async def ok(tr, *a, **k):
        return {"model": "m", "steps": 2, "tool_calls": 1, "tokens": 99, "llm_errors": 0}

    monkeypatch.setattr(agent, "_investigate", ok)
    monkeypatch.setenv("SRE_LANGTRACE", "1")
    out = asyncio.run(agent.investigate("m"))
    assert out["tokens"] == 99
    root = next(o for o in fake_langfuse.observations if o.name == "investigation")
    assert root.updates[-1]["output"]["tokens"] == 99


def test_investigate_flag_off_records_nothing(fake_langfuse, monkeypatch):
    async def ok(tr, *a, **k):
        return {"model": "m", "tokens": 1}

    monkeypatch.setattr(agent, "_investigate", ok)
    monkeypatch.delenv("SRE_LANGTRACE", raising=False)
    asyncio.run(agent.investigate("m"))
    assert fake_langfuse.observations == [] and fake_langfuse.flushed == 0


def test_server_env_has_only_expected_keys(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "not-real")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "not-real")
    env = agent.build_server_env("r1", True)
    assert set(env) <= {"PATH", "HOME", "SRE_RUN_ID", "SRE_PROPOSE"}
    assert env["SRE_RUN_ID"] == "r1" and env["SRE_PROPOSE"] == "1"
