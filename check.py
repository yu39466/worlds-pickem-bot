"""Dev helper for poking at the MCP tools. Not part of the bot.

    uv run check.py                                      list every tool
    uv run check.py get_game_length_extremes             call one, no arguments
    uv run check.py get_champion_stats '{"limit": 3}'    call one with arguments

Talks to the server in-process, so there is no subprocess and no Claude
involved - just your tool code. Prints the description exactly as Claude
would see it, plus the result and its size in bytes.
"""

import asyncio
import json
import sys

from fastmcp import Client

import server


async def main():
    name = sys.argv[1] if len(sys.argv) > 1 else None
    args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}

    async with Client(server.mcp) as client:
        tools = await client.list_tools()

        if not name:
            for tool in tools:
                params = tool.input_schema.get("properties", {})
                print(f"\n=== {tool.name}({', '.join(params) or ''})")
                print(tool.description)
            print(f"\n{len(tools)} tool(s)")
            return

        match = [t for t in tools if t.name == name]
        if not match:
            print(f"no tool named {name!r}. available: {[t.name for t in tools]}")
            return

        print("=== description Claude sees ===")
        print(match[0].description)
        print("\n=== parameters ===")
        print(json.dumps(match[0].input_schema.get("properties", {}), indent=2))

        print("\n=== result ===")
        result = await client.call_tool(name, args)
        body = json.dumps(result.data, indent=2)
        print(body)
        print(f"\nbytes: {len(json.dumps(result.data))}")


asyncio.run(main())
