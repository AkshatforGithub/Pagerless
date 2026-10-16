import asyncio, json, sys, time
from pathlib import Path
from typing import Literal
from fastmcp import Client
from groq import AsyncGroq, BadRequestError, RateLimitError
from pydantic import BaseModel, Field, ValidationError

ROOT = Path(__file__).resolve().parent
from dotenv import load_dotenv; load_dotenv(ROOT / ".env")
ALERT = "ALERT: the web service at localhost:8000 is unhealthy or degraded. Find the root cause."


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
    for attempt in range(6):
        try:
            return await client.chat.completions.create(**kw)
        except RateLimitError:
            print("rate limited, retrying", attempt + 1, file=sys.stderr, flush=True)
            await asyncio.sleep(10 * (attempt + 1))
    raise RuntimeError("rate limited too many times")


async def call_mcp(mcp, call, stats):
    try:
        args = json.loads(call.function.arguments or "{}")
        r = await mcp.call_tool(call.function.name, args)
        return r.content[0].text if r.content else "(empty)"
    except Exception as e:
        stats["tool_errors"] += 1
        return f"ERROR: tool call failed: {e}"


async def investigate(model, prompt_version="v1", alert=ALERT, max_steps=10, token_budget=60000):
    client = AsyncGroq()  # reads GROQ_API_KEY from the environment
    system = (ROOT / "prompts" / f"{prompt_version}.txt").read_text()
    stats = dict(steps=0, tool_calls=0, tool_errors=0, llm_errors=0, tokens=0, forced=0)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": alert}]
    diagnosis, t0, forced_notice = None, time.time(), False
    async with Client(ROOT / "mcp_server.py") as mcp:
        tools = [to_openai(t) for t in await mcp.list_tools()] + [SUBMIT]
        for step in range(max_steps + 3):
            force = step >= max_steps or stats["tokens"] >= token_budget
            choice = "required" if force else "auto"
            if force and not forced_notice:
                forced_notice = True
                stats["forced"] = 1
                messages.append({"role": "user", "content": "Step limit reached. No more investigation is possible. Call submit_diagnosis now with the best-supported root cause from the evidence so far, even if confidence is low."})
            stats["steps"] += 1
            try:
                resp = await chat(client, model=model, messages=messages, tools=[SUBMIT] if force else tools,
                                  tool_choice=choice, temperature=0)
            except BadRequestError as e:
                print("LLM error:", str(e)[:400], file=sys.stderr, flush=True)
                stats["llm_errors"] += 1
                messages.append({"role": "user", "content": "Call submit_diagnosis now with your best hypothesis." if force else "Your last tool call was malformed. Call a tool again with valid JSON arguments."})
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
