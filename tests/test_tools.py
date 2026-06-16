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

BASE = "http://booklore.test"


@pytest.fixture
def authed(respx_mock):
    """Mock login and give each test a fresh client.

    The server's lifespan closes the shared HTTP client when the in-memory
    `Client(mcp)` context exits, so we reinstate a fresh, unauthenticated client
    per test rather than reusing a closed one.
    """
    respx_mock.post(f"{BASE}/api/v1/auth/login").mock(
        return_value=httpx.Response(200, json={"accessToken": "acc1", "refreshToken": "ref1"})
    )
    server.client = server.BookLoreClient(BASE, "tester", "secret")
    return respx_mock


async def test_list_books_filters_and_limits(authed):
    books = [
        {"id": 1, "title": "The Hobbit", "metadata": {"authors": [{"name": "J.R.R. Tolkien"}]}},
        {"id": 2, "title": "Dune", "metadata": {"authors": [{"name": "Frank Herbert"}]}},
    ]
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))

    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool("list_books", {"query": "tolkien"})

    # list_books returns list[BookSummary]; FastMCP deserializes structured
    # output into model objects, so fields are attributes, not dict keys.
    assert len(result.data) == 1
    assert result.data[0].id == 1


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


async def test_delete_shelf_returns_confirmation(authed):
    authed.delete(f"{BASE}/api/v1/shelves/3").mock(return_value=httpx.Response(204))

    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool("delete_shelf", {"shelf_id": 3})

    assert "Deleted shelf 3" in result.data
