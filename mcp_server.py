import os
import sys
from fastmcp import FastMCP
import tools

mcp = FastMCP(
    "pagerless",
    instructions="Read-only diagnostic tools for a small Docker stack (web, postgres, redis). "
                 "Nothing here changes state.",
)
# Base 6 read-only tools (always available for baseline comparability)
_base_tools = (tools.get_logs, tools.container_status, tools.disk_usage,
               tools.query_metrics, tools.probe_endpoints, tools.run_diagnostic)
# SRE_PROPOSE=1 enables the 7th proposal tool; unset keeps the original 6
_sre_propose = os.environ.get("SRE_PROPOSE", "0") == "1"
# Per-investigation run-id; if unset the one-pending-per-run cap is disabled
_sre_run_id = os.environ.get("SRE_RUN_ID")
_all_tools = _base_tools + (tools.propose_action,) if _sre_propose and _sre_run_id else _base_tools
for fn in _all_tools:
    mcp.tool(fn)

# Output tool-list verification to stderr so it does not leak into stdout
_baseline_names = [fn.__name__ for fn in _base_tools]
_augmented_names = [fn.__name__ for fn in _all_tools]
print(f"SRE_PROPOSE={os.environ.get('SRE_PROPOSE', 'not set')}: baseline={_baseline_names}", file=sys.stderr)
print(f"augmented={_augmented_names}", file=sys.stderr)

if __name__ == "__main__":
    mcp.run()
