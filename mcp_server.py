from fastmcp import FastMCP
import tools

mcp = FastMCP(
    "pagerless",
    instructions="Read-only diagnostic tools for a small Docker stack (web, postgres, redis). "
                 "Nothing here changes state.",
)
for fn in (tools.get_logs, tools.container_status, tools.disk_usage,
           tools.query_metrics, tools.probe_endpoints, tools.run_diagnostic):
    mcp.tool(fn)

if __name__ == "__main__":
    mcp.run()
