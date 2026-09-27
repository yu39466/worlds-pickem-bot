"""MCP server exposing Worlds pick'ems data."""

import logging
import sys

import anyio
from fastmcp import FastMCP

from sources import cache
from sources import leaguepedia as lp

logging.basicConfig(stream=sys.stderr, level=logging.INFO)
log = logging.getLogger("lina.server")

mcp = FastMCP("lina-worlds")

# tools go here

@mcp.tool
async def get_game_length_extremes() -> dict:
    """"""

if __name__ == "__main__":
    mcp.run()