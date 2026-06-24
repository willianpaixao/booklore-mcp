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


async def test_delete_shelf_returns_confirmation(authed):
    authed.delete(f"{BASE}/api/v1/shelves/3").mock(return_value=httpx.Response(204))

    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool("delete_shelf", {"shelf_id": 3})

    assert "Deleted shelf 3" in result.data


async def test_list_libraries_trims_fields(authed):
    libs = [
        {
            "id": 1,
            "name": "Ebooks",
            "watch": True,
            "paths": [{"id": 10, "libraryId": 1, "path": "/data/ebooks"}],
            "allowedFormats": ["EPUB", "PDF"],
            "metadataSource": "EMBEDDED",
        }
    ]
    authed.get(f"{BASE}/api/v1/libraries").mock(return_value=httpx.Response(200, json=libs))

    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool("list_libraries", {})

    assert result.data == [
        {
            "id": 1,
            "name": "Ebooks",
            "watch": True,
            "paths": [{"id": 10, "path": "/data/ebooks"}],
            "allowed_formats": ["EPUB", "PDF"],
        }
    ]


async def test_refresh_library_triggers_rescan(authed):
    route = authed.put(f"{BASE}/api/v1/libraries/7/refresh").mock(
        return_value=httpx.Response(200)
    )

    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool("refresh_library", {"library_id": 7})

    assert route.called
    assert result.data["library_id"] == 7
    assert "rescan" in result.data["status"]


async def test_bookdrop_rescan_triggers_scan(authed):
    route = authed.post(f"{BASE}/api/v1/bookdrop/rescan").mock(
        return_value=httpx.Response(200)
    )

    async with Client(server.mcp) as mcp_client:
        result = await mcp_client.call_tool("bookdrop_rescan", {})

    assert route.called
    assert "bookdrop" in result.data["status"]
