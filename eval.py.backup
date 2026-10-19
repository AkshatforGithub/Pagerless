import argparse, asyncio, hashlib, json, os, re, signal, subprocess, sys, time
from pathlib import Path
import yaml
from groq import AsyncGroq
from agent import chat, investigate

ROOT = Path(__file__).resolve().parent
CACHE = ROOT / "results" / "judge_cache.json"
JUDGE_PROMPT = """You grade an SRE agent's root-cause diagnosis.
TRUE ROOT CAUSE: {truth}
AGENT DIAGNOSIS: {diag}
Judge only the root_cause field; the evidence field is context. Is the agent's root cause the same underlying problem as the true root cause? A symptom alone (for example 'requests fail with 500' or 'the container exited') is NOT correct. Extra detail is fine. If the agent asserts a specific cause that the true root cause does not mention or imply (for example a missing variable, a bad config, a resource problem or a false-alarm claim), it is incorrect even when the symptom matches.
Reply with JSON only: {{"reason": "<one sentence>", "correct": true}} or {{"reason": "<one sentence>", "correct": false}}"""


def sh(*cmd):
    return subprocess.run(list(cmd), cwd=ROOT, capture_output=True, text=True)


async def judge_one(client, judge, truth, diag, cache):
    key = hashlib.sha256(json.dumps([judge, truth, diag], sort_keys=True).encode()).hexdigest()
    if key in cache:
        return cache[key]
    resp = await chat(client, model=judge, temperature=0, messages=[
        {"role": "user", "content": JUDGE_PROMPT.format(truth=truth, diag=json.dumps(diag))}])
    m = re.search(r"\{.*\}", resp.choices[0].message.content or "", re.S)
    try:
        v = json.loads(m.group(0))
        out = {"correct": bool(v["correct"]), "reason": str(v.get("reason", ""))}
    except Exception:
        return {"correct": False, "reason": "judge output unparseable"}
    cache[key] = out
    return out


def kw_hit(fault, diag):
    text = str(diag.get("root_cause", "")).lower()
    return any(str(k).lower() in text for k in fault["keywords"])


async def trial(fault, model, prompt, judge, client, cache, settle):
    if sh("./reset.sh").returncode:
        sys.exit("reset.sh failed: stack unhealthy, fix it before evaluating")
    sh("./inject.sh", fault["id"])
    await asyncio.sleep(settle)
    load = subprocess.Popen(["./loadgen.sh", "180"], cwd=ROOT, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        res = await investigate(model, prompt)
    finally:
        os.killpg(load.pid, signal.SIGTERM)
    d = res["diagnosis"]
    if d is None:
        verdict = {"correct": False, "reason": "no diagnosis submitted"}
    else:
        verdict = await judge_one(client, judge, fault["root_cause"], d, cache)
    return {**res, "fault": fault["id"], "correct": verdict["correct"],
            "judge_reason": verdict["reason"], "kw_hit": bool(d) and kw_hit(fault, d)}


def summarize(rows):
    groups = {}
    for r in rows:
        groups.setdefault((r["model"], r["prompt"]), []).append(r)
    print(f"\n{'model':34}{'prompt':8}{'acc':>6}{'kw=judge':>10}{'steps':>7}{'tools':>7}{'tokens':>9}{'secs':>7}")
    for (m, p), rs in groups.items():
        n = len(rs)
        avg = lambda k: sum(r[k] for r in rs) / n
        print(f"{m:34}{p:8}{sum(r['correct'] for r in rs) / n:>6.0%}"
              f"{sum(r['correct'] == r['kw_hit'] for r in rs) / n:>10.0%}"
              f"{avg('steps'):>7.1f}{avg('tool_calls'):>7.1f}{avg('tokens'):>9.0f}{avg('seconds'):>7.1f}")
        wrong = sorted({r["fault"] for r in rs if not r["correct"]})
        if wrong:
            print(f"   wrong: {', '.join(wrong)}")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True, help="comma-separated Groq model ids")
    ap.add_argument("--judge", required=True, help="Groq model id used as judge")
    ap.add_argument("--prompt", default="v1")
    ap.add_argument("--faults", default="", help="comma-separated fault ids (default: all)")
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--settle", type=int, default=10)
    ap.add_argument("--skip-done", action="store_true", help="skip faults already saved for this model and prompt")
    ap.add_argument("--max-trials", type=int, default=0, help="stop after this many new faults (0 = no limit)")
    a = ap.parse_args()
    faults = yaml.safe_load(open(ROOT / "faults" / "faults.yaml"))
    if a.faults:
        faults = [f for f in faults if f["id"] in a.faults.split(",")]
    out = ROOT / "results" / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    out.parent.mkdir(exist_ok=True)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    client, rows = AsyncGroq(), []
    done = set()
    ran = 0
    if a.skip_done:
        for fn in (ROOT / "results").glob("2*.jsonl"):
            for line in fn.read_text().splitlines():
                r = json.loads(line)
                if r["prompt"] == a.prompt:
                    done.add((r["model"], r["fault"]))
    for model in a.models.split(","):
        for f in faults:
            if (model, f["id"]) in done:
                print("skip (already done):", model, f["id"])
                continue
            if a.max_trials and ran >= a.max_trials:
                print("max-trials reached; rerun later with --skip-done")
                break
            ran += 1
            for _ in range(a.runs):
                row = await trial(f, model, a.prompt, a.judge, client, cache, a.settle)
                rows.append(row)
                out.open("a").write(json.dumps(row) + "\n")
                CACHE.write_text(json.dumps(cache))
                print(f"{model} {f['id']:18} correct={row['correct']} steps={row['steps']} tokens={row['tokens']}")
    sh("./reset.sh")
    summarize(rows)
    print(f"\nrows saved to {out}")

if __name__ == "__main__":
    asyncio.run(main())
