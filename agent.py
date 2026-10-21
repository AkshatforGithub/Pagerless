import asyncio, json, sys, time, uuid, functools
from pathlib import Path
from typing import Literal
from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport
from groq import AsyncGroq, BadRequestError, RateLimitError
from pydantic import BaseModel, Field, ValidationError

ROOT = Path(__file__).resolve().parent
from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
ALERT = "ALERT: the web service at localhost:8000 is unhealthy or degraded. Find the root cause."

# Safety: agent only has propose_path via MCP, no direct access to execute/approve
from actions import propose  # noqa: F401 — imported for proposal flow only


class Diagnosis(BaseModel):
    root_cause: str = Field(min_length=10)
    evidence: list[str] = Field(min_length=1)
    proposed_fix: str
    confidence: Literal["low", "medium", "high"]


SUBMIT = {"type": "function", "function": {
    "name": "submit_diagnosis",
    "description": "Submit the final diagnosis. Call exactly once, when done.",
    "parameters": {"type": "object", "properties": {
        "root_cause": {"type": "string", "description": "The specific underlying cause, not the symptom"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "proposed_fix": {"type": "string"},
        "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
        "required": ["root_cause", "evidence", "proposed_fix", "confidence"]}}}


def to_openai(t):
    return {"type": "function", "function": {
        "name": t.name, "description": t.description or "", "parameters": t.input_schema}}


async def chat(client, **kw):
    for attempt in range(8):
        try:
            return await client.chat.completions.create(**kw)
        except RateLimitError as e:
            print("rate limited:", str(e)[:300], file=sys.stderr, flush=True)
            if "per day" in str(e):
                raise RuntimeError("daily token limit reached; stop and resume later") from e
            await asyncio.sleep(min(15 * (attempt + 1), 90))
    raise RuntimeError("rate limited too many times")


async def call_mcp(mcp, call, stats):
    try:
        args = json.loads(call.function.arguments or "{}")
        r = await mcp.call_tool(call.function.name, args)
        return r.content[0].text if r.content else "(empty)"
    except Exception as e:
        stats["tool_errors"] += 1
        return f"ERROR: tool call failed: {e}"


def build_server_env(run_id, propose):
    """Environment for the tool server: only what it needs, never the API key."""
    import os
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env["SRE_RUN_ID"] = run_id
    if propose:
        env["SRE_PROPOSE"] = "1"
    return env


def make_server_transport(run_id, propose):
    return PythonStdioTransport(ROOT / "mcp_server.py", env=build_server_env(run_id, propose))


async def investigate(model, prompt_version="v1", alert=ALERT, max_steps=10, token_budget=60000):
    import os
    client = AsyncGroq()  # reads GROQ_API_KEY from the environment
    system = (ROOT / "prompts" / f"{prompt_version}.txt").read_text()
    # Per-run investigation ID, also passed to mcp_server.py via SRE_RUN_ID
    run_id = uuid.uuid4().hex[:16]
    # Bind the run_id to propose_action in tools.py via the module-level variable
    from tools import _set_propose_action_run_id
    _set_propose_action_run_id(run_id)
    stats = dict(steps=0, tool_calls=0, tool_errors=0, llm_errors=0, tokens=0, forced=0,
                 run_id=run_id)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": alert}]
    diagnosis, t0, forced_notice, temp = None, time.time(), False, 0
    async with Client(make_server_transport(run_id, os.environ.get("SRE_PROPOSE") == "1")) as mcp:
        tools = [to_openai(t) for t in await mcp.list_tools()] + [SUBMIT]
        for step in range(max_steps + 6):
            force = step >= max_steps or stats["tokens"] >= token_budget
            choice = "required" if force else "auto"
            if force and not forced_notice:
                forced_notice = True
                stats["forced"] = 1
                messages.append({"role": "user", "content": "Step limit reached. No more investigation is possible. Call submit_diagnosis now with the best-supported root cause from the evidence so far, even if confidence is low."})
            stats["steps"] += 1
            try:
                resp = await chat(client, model=model, messages=messages, tools=[SUBMIT] if force else tools,
                                  tool_choice=choice, temperature=temp)
            except BadRequestError as e:
                print("LLM error:", str(e)[:400], file=sys.stderr, flush=True); stats["steps"] -= 1; temp = 0.5 if force else temp
                stats["llm_errors"] += 1
                messages.append({"role": "user", "content": "Your last reply was not a tool call. Reply with ONLY a submit_diagnosis tool call, no prose." if force else "Your last tool call was malformed. Call a tool again with valid JSON arguments."})
                continue
            stats["tokens"] += resp.usage.total_tokens
            msg = resp.choices[0].message
            print("step", step, "tokens", stats["tokens"], [c.function.name for c in (msg.tool_calls or [])], file=sys.stderr, flush=True)
            if not msg.tool_calls:
                messages.append({"role": "assistant", "content": msg.content or ""})
                messages.append({"role": "user", "content": "Continue with tools, or call submit_diagnosis if you have enough evidence."})
                continue
            messages.append({"role": "assistant", "content": msg.content or "", "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in msg.tool_calls]})
            for c in msg.tool_calls:
                if c.function.name == "submit_diagnosis":
                    try:
                        diagnosis = Diagnosis.model_validate_json(c.function.arguments)
                        break
                    except ValidationError:
                        stats["llm_errors"] += 1
                        result = "ERROR: invalid diagnosis format, fix it and resubmit"
                else:
                    stats["tool_calls"] += 1
                    result = await call_mcp(mcp, c, stats)
                messages.append({"role": "tool", "tool_call_id": c.id, "content": result})
            if diagnosis:
                break
    return {"model": model, "prompt": prompt_version,
            "diagnosis": diagnosis.model_dump() if diagnosis else None,
            "seconds": round(time.time() - t0, 1), **stats}


if __name__ == "__main__":
    print(json.dumps(asyncio.run(investigate(sys.argv[1], *sys.argv[2:3])), indent=1))
