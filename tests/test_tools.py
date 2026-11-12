"""End-to-end tool tests through FastMCP's in-memory client.

Each tool call flows through the real MCP layer into the module-level client,
whose HTTP calls are intercepted by respx.
"""

from __future__ import annotations

import httpx
from fastmcp import Client

import server

BASE = "http://bookorbit.test"


async def test_refresh_library_triggers_rescan(authed):
    route = authed.put(f"{BASE}/api/v1/libraries/7/refresh").mock(return_value=httpx.Response(200))

    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool("refresh_library", {"library_id": 7})

    assert route.called
    assert result.data["library_id"] == 7
    assert "rescan" in result.data["status"]


async def test_bookdrop_rescan_triggers_scan(authed):
    route = authed.post(f"{BASE}/api/v1/bookdrop/rescan").mock(return_value=httpx.Response(200))

    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool("bookdrop_rescan", {})

    assert route.called
    assert "bookdrop" in result.data["status"]
