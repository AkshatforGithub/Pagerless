import json, sys
for f in sys.argv[1:]:
    print("==", f)
    for r in map(json.loads, open(f)):
        d = r["diagnosis"] or {}
        print(r["fault"], "| correct:", r["correct"], "| kw:", r["kw_hit"], "| steps:", r["steps"], "| forced:", r["forced"], "| tokens:", r["tokens"])
        print("   cause:", (d.get("root_cause") or "NONE")[:160])
        print("   judge:", r["judge_reason"][:160])
