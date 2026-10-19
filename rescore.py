import asyncio, json, sys
import yaml
from groq import AsyncGroq
from eval import judge_one, kw_hit, summarize, CACHE, ROOT


async def main(judge, files):
    faults = {f["id"]: f for f in yaml.safe_load(open(ROOT / "faults" / "faults.yaml"))}
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    client, rows = AsyncGroq(), []
    for fn in files:
        for r in map(json.loads, open(fn)):
            # Skip re-scoring injection_failed rows, but keep them in output
            if r.get("injection_failed", False):
                rows.append(r)
                continue

            f, d = faults[r["fault"]], r["diagnosis"]
            if d is None:
                v = {"correct": False, "reason": "no diagnosis submitted"}
            else:
                v = await judge_one(client, judge, f["root_cause"], d, cache)
            r.update(correct=v["correct"], judge_reason=v["reason"], kw_hit=bool(d) and kw_hit(f, d))
            rows.append(r)
            CACHE.write_text(json.dumps(cache))
    out = ROOT / "results" / "rescored.jsonl"
    out.write_text("".join(json.dumps(r) + "\n" for r in rows))
    summarize(rows)
    print("saved", out)

asyncio.run(main(sys.argv[1], sys.argv[2:]))