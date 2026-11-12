"""End-to-end tool tests through FastMCP's in-memory client.

Each tool call flows through the real MCP layer into the module-level client,
whose HTTP calls are intercepted by respx.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

import server

BASE = "http://bookorbit.test"


async def test_update_metadata_always_sends_clear_flags(authed):
    route = authed.put(f"{BASE}/api/v1/books/5/metadata").mock(
        return_value=httpx.Response(200, json={"title": "X"})
    )

    async with Client(server.mcp) as mcp_client:
        await mcp_client.call_tool(
            "update_book_metadata",
            {"book_id": 5, "metadata": {"title": "X"}, "merge_categories": True},
        )

    request = route.calls.last.request
    body = json.loads(request.content)
    assert body["clearFlags"] == {}  # documented server-500 workaround
    assert body["metadata"] == {"title": "X"}
    # Default is the safe field-merge mode, not the whole-record-wipe REPLACE_ALL.
    assert request.url.params["replaceMode"] == "REPLACE_WHEN_PROVIDED"
    assert request.url.params["mergeCategories"] == "true"


async def test_update_metadata_clear_fields_and_normalizes_goodreads_id(authed):
    route = authed.put(f"{BASE}/api/v1/books/5/metadata").mock(
        return_value=httpx.Response(200, json={})
    )
    async with Client(server.mcp) as mcp_client:
        await mcp_client.call_tool(
            "update_book_metadata",
            {
                "book_id": 5,
                "metadata": {"goodreadsId": "23463279-designing-data-intensive"},
                "clear_fields": ["amazonRating"],
            },
        )

    body = json.loads(route.calls.last.request.content)
    # clear_fields -> a true clearFlag, the only way to null one field under the safe mode.
    assert body["clearFlags"] == {"amazonRating": True}
    # goodreadsId slug normalized to its bare numeric form on write.
    assert body["metadata"]["goodreadsId"] == "23463279"


async def test_update_metadata_rejects_unknown_clear_field(authed):
    async with Client(server.mcp) as mcp_client:
        with pytest.raises(Exception, match="Cannot clear unknown field"):
            await mcp_client.call_tool(
                "update_book_metadata",
                {"book_id": 5, "metadata": {}, "clear_fields": ["bogus"]},
            )


async def test_bulk_update_metadata_writes_arbitrary_fields(authed):
    authed.get(f"{BASE}/api/v1/books/3").mock(
        return_value=httpx.Response(200, json={"id": 3, "metadata": {}})
    )
    put = authed.put(f"{BASE}/api/v1/books/3/metadata").mock(
        return_value=httpx.Response(200, json={})
    )
    async with Client(server.mcp) as mcp_client:
        await mcp_client.call_tool(
            "bulk_update_metadata",
            {"book_ids": [3], "patch": {"metadata": {"isbn13": "999", "language": "en"}}},
        )

    body = json.loads(put.calls.last.request.content)
    assert body["metadata"] == {"isbn13": "999", "language": "en"}


async def test_bulk_update_metadata_reports_partial_failure_in_order(authed):
    for bid in (3, 4):
        authed.get(f"{BASE}/api/v1/books/{bid}").mock(
            return_value=httpx.Response(200, json={"id": bid, "metadata": {}})
        )
    authed.put(f"{BASE}/api/v1/books/3/metadata").mock(return_value=httpx.Response(200, json={}))
    authed.put(f"{BASE}/api/v1/books/4/metadata").mock(return_value=httpx.Response(500))
    patch = {"metadata": {"language": "en"}}
    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool(
            "bulk_update_metadata",
            {
                "items": [
                    {"book_id": 3, "patch": patch},
                    {"book_id": "x", "patch": patch},
                    {"book_id": 4, "patch": patch},
                ]
            },
        )

    assert result.data["updated"] == [3]
    assert [f["book_id"] for f in result.data["failed"]] == ["x", 4]
    assert "integer 'book_id'" in result.data["failed"][0]["error"]


async def test_delete_shelf_returns_confirmation(authed):
    authed.delete(f"{BASE}/api/v1/shelves/3").mock(return_value=httpx.Response(204))

    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool("delete_shelf", {"shelf_id": 3})

    assert "Deleted shelf 3" in result.data


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
