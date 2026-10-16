import asyncio
from fastmcp import Client


async def main():
    async with Client("mcp_server.py") as c:
        print([t.name for t in await c.list_tools()])
        for name, args in [("container_status", {"name": "web"}),
                           ("run_diagnostic", {"name": "pg_indexes"})]:
            r = await c.call_tool(name, args)
            print(f"\n=== {name}\n{r.content[0].text}")

asyncio.run(main())
