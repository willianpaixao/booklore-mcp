"""Tests for the search/discovery helpers and the new search/bulk/additive tools."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

import server

BASE = "http://booklore.test"


# --- pure helper unit tests (no I/O) ----------------------------------------


def test_author_names_normalizes_dicts_and_strings():
    meta = {"authors": [{"name": "Ann"}, "Bob", {"name": None}, ""]}
    assert server._author_names(meta) == ["Ann", "Bob"]


def test_merge_list_add_remove_case_insensitive_preserves_order():
    merged = server._merge_list(
        ["Python", "narrative"], add=["python", "trading"], remove=["NARRATIVE"]
    )
    assert merged == ["Python", "trading"]


def test_matches_filters_tags_any_vs_all():
    book = {"metadata": {"tags": ["blockchain", "bitcoin"]}}
    assert server._matches_filters(book, {"tags": ["BITCOIN"]})  # any, case-insensitive
    assert server._matches_filters(book, {"tags": ["bitcoin", "blockchain"], "tags_mode": "all"})
    assert not server._matches_filters(book, {"tags": ["bitcoin", "ethereum"], "tags_mode": "all"})


def test_matches_filters_missing_requires_every_listed_field_empty():
    book = {"metadata": {"tags": ["x"], "categories": []}}
    assert server._matches_filters(book, {"missing": ["categories"]})
    assert not server._matches_filters(book, {"missing": ["tags"]})
    assert not server._matches_filters(book, {"missing": ["tags", "categories"]})


def test_matches_filters_shelf_library_status():
    book = {"libraryId": 1, "readStatus": "READ", "shelves": [{"id": 5}], "metadata": {}}
    assert server._matches_filters(book, {"shelf_ids": [5]})
    assert not server._matches_filters(book, {"shelf_ids": [9]})
    assert server._matches_filters(book, {"library_ids": [1]})
    assert server._matches_filters(book, {"read_status": "READ"})
    assert not server._matches_filters(book, {"read_status": "UNREAD"})


def test_matches_query_over_title_and_authors():
    book = {"title": "The Hobbit", "metadata": {"authors": ["J.R.R. Tolkien"]}}
    assert server._matches_query(book, "tolkien")
    assert server._matches_query(book, "hobbit")
    assert not server._matches_query(book, "dune")


# --- tool flows -------------------------------------------------------------


@pytest.fixture
def authed(respx_mock):
    respx_mock.post(f"{BASE}/api/v1/auth/login").mock(
        return_value=httpx.Response(200, json={"accessToken": "a", "refreshToken": "r"})
    )
    server.client = server.BookLoreClient(BASE, "tester", "secret")
    return respx_mock


_BOOKS = [
    {"id": 1, "title": "Tagged", "libraryId": 1, "metadata": {"tags": ["x"], "authors": ["Ann"]}},
    {"id": 2, "title": "Untagged", "libraryId": 1, "metadata": {"tags": [], "authors": ["Bob"]}},
]


async def test_search_books_missing_filter(authed):
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_BOOKS))
    async with Client(server.mcp) as c:
        result = await c.call_tool("search_books", {"filters": {"missing": ["tags"]}})
    assert result.data.total == 1
    assert result.data.books[0].id == 2


async def test_add_tags_merges_existing_and_puts(authed):
    book = {"id": 7, "metadata": {"tags": ["keep"]}}
    authed.get(f"{BASE}/api/v1/books/7").mock(return_value=httpx.Response(200, json=book))
    put = authed.put(f"{BASE}/api/v1/books/7/metadata").mock(
        return_value=httpx.Response(200, json={})
    )

    async with Client(server.mcp) as c:
        await c.call_tool("add_tags", {"book_id": 7, "tags": ["new", "keep"]})

    body = json.loads(put.calls.last.request.content)
    assert body["metadata"]["tags"] == ["keep", "new"]  # merged, deduped, order preserved
    assert body["clearFlags"] == {}
    # Must touch ONLY tags — REPLACE_ALL would wipe title/authors/description/ISBNs.
    assert put.calls.last.request.url.params["replaceMode"] == "REPLACE_WHEN_PROVIDED"


async def test_bulk_update_reports_partial_success(authed):
    authed.get(f"{BASE}/api/v1/books/1").mock(
        return_value=httpx.Response(200, json={"id": 1, "metadata": {"tags": []}})
    )
    authed.put(f"{BASE}/api/v1/books/1/metadata").mock(return_value=httpx.Response(200, json={}))
    authed.get(f"{BASE}/api/v1/books/999").mock(return_value=httpx.Response(404, text="nope"))

    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "bulk_update_metadata",
            {"book_ids": [1, 999], "patch": {"tags": {"add": ["t"]}}},
        )

    data = result.data
    assert data["updated"] == [1]
    assert data["failed"][0]["book_id"] == 999


async def test_ping_reports_authenticated_with_version_and_libraries(authed):
    authed.get(f"{BASE}/api/v1/healthcheck").mock(
        return_value=httpx.Response(200, json={"data": {"version": "1.2.3"}})
    )
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=_BOOKS))
    authed.get(f"{BASE}/api/v1/libraries").mock(return_value=httpx.Response(200, json=[{"id": 1}]))

    async with Client(server.mcp) as c:
        result = await c.call_tool("ping", {})

    d = result.data
    assert d["ok"] is True and d["authenticated"] is True
    assert d["book_count"] == 2
    assert d["server_version"] == "1.2.3"
    assert d["library_count"] == 1


async def test_set_field_locks_builds_field_actions(authed):
    route = authed.put(f"{BASE}/api/v1/books/metadata/toggle-field-locks").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with Client(server.mcp) as c:
        await c.call_tool(
            "set_field_locks",
            {"book_ids": [5], "lock": ["tags", "authors"], "unlock": ["description"]},
        )
    body = json.loads(route.calls.last.request.content)
    assert body["bookIds"] == [5]
    assert body["fieldActions"] == {
        "tagsLocked": "LOCK",
        "authorsLocked": "LOCK",
        "descriptionLocked": "UNLOCK",
    }


async def test_list_authors_uses_authors_endpoint(authed):
    authed.get(f"{BASE}/api/v1/authors").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "name": "Ann", "bookCount": 3},
                {"id": 2, "name": "Bob", "bookCount": 1},
            ],
        )
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool("list_authors", {"query": "an"})

    d = result.data
    assert d["total"] == 1
    assert d["authors"][0]["name"] == "Ann"
    assert d["authors"][0]["book_count"] == 3


async def test_fetch_metadata_candidates_parses_sse_stream(authed):
    sse = 'data: {"title": "Cand A"}\n\ndata: {"title": "Cand B"}\n\n'
    authed.post(f"{BASE}/api/v1/books/7/metadata/prospective").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "fetch_metadata_candidates", {"book_id": 7, "providers": ["Google"]}
        )

    d = result.data
    assert d["count"] == 2
    assert {c["title"] for c in d["candidates"]} == {"Cand A", "Cand B"}
