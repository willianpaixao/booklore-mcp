"""Opt-in, read-only checks against a real BookOrbit server.

Skipped unless BOOKORBIT_IT_URL is set. They use their own variables because
tests/conftest.py forces BOOKORBIT_URL to a fake host for the offline suite.

    BOOKORBIT_IT_URL=https://books.example.com \\
    BOOKORBIT_IT_API_KEY=bok_... \\
    uv run pytest -m integration

These tests must only ever read: never add a test that writes to the server.
"""

from __future__ import annotations

import os

import pytest

import server
from server import BookOrbitClient, all_of, any_of, build_book_query, rule, sort_by

IT_URL = os.environ.get("BOOKORBIT_IT_URL", "").rstrip("/")
IT_API_KEY = os.environ.get("BOOKORBIT_IT_API_KEY", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not IT_URL, reason="set BOOKORBIT_IT_URL to run live tests"),
]

needs_key = pytest.mark.skipif(not IT_API_KEY, reason="set BOOKORBIT_IT_API_KEY")


async def test_health_is_up():
    client = BookOrbitClient(IT_URL, None, timeout=30)
    try:
        resp = await client._send("GET", "/api/v1/health")
    finally:
        await client.close()
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@needs_key
async def test_api_key_authenticates():
    client = BookOrbitClient(IT_URL, IT_API_KEY, timeout=30)
    try:
        me = await client.get("/api/v1/auth/me")
    finally:
        await client.close()
    assert me["authenticationMethod"] == "api_key"


# --- book queries (POST routes BookOrbit marks read-safe for API keys) ---------


@pytest.fixture
async def live():
    client = BookOrbitClient(IT_URL, IT_API_KEY, timeout=60)
    try:
        yield client
    finally:
        await client.close()


@needs_key
async def test_unfiltered_query_total_matches_library_stats(live):
    page = await live.query_books(build_book_query(size=1))
    stats = await live.get("/api/v1/statistics/summary")
    assert page["total"] == stats["totalBooks"]


@needs_key
async def test_paging_returns_distinct_books_up_to_the_bound(live):
    ids = []
    async for page in live.iter_book_pages(
        max_books=7, page_size=3, sort=[sort_by("title"), sort_by("addedAt", "desc")]
    ):
        ids += [b["id"] for b in page["items"]]
    expected = min(7, page["total"])
    assert len(ids) == expected and len(set(ids)) == expected


@needs_key
async def test_server_accepts_client_validated_filters(live):
    f = all_of(
        any_of(rule("isbn", "isEmpty"), rule("genre", "isNotEmpty")),
        rule("readStatus", "excludesAll", ["abandoned"]),
        rule("addedAt", "before", "2999-01-01"),
    )
    page = await live.query_books(build_book_query(filter=f, size=2))
    for card in page["items"]:
        assert server._summarize_card(card).id == card["id"]


@needs_key
async def test_library_and_collection_scoped_queries(live):
    libraries = await live.get("/api/v1/libraries")
    if libraries:
        page = await live.query_books(build_book_query(size=1), library_id=libraries[0]["id"])
        assert "total" in page
    collections = await live.get("/api/v1/collections")
    if collections:
        query = build_book_query(size=1, sort=[sort_by("collectionOrder")], collection_scoped=True)
        page = await live.query_books(query, collection_id=collections[0]["id"])
        assert page["total"] == collections[0]["bookCount"]


# --- read tools, through the MCP layer -----------------------------------------


@pytest.fixture
def live_tools(monkeypatch):
    monkeypatch.setattr(server, "client", BookOrbitClient(IT_URL, IT_API_KEY, timeout=60))


@needs_key
async def test_reading_stats_statuses_add_up_to_total(live_tools):
    from fastmcp import Client

    async with Client(server.mcp) as c:
        stats = (await c.call_tool("get_reading_stats", {})).data
    assert sum(stats["by_status"].values()) == stats["total"]
    assert stats["rated_count"] == sum(stats["ratings"].values())


@needs_key
async def test_search_missing_filter_matches_direct_query(live_tools):
    from fastmcp import Client

    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "search_books",
            {"filters": {"missing": ["isbn", "amazonRating"], "missing_mode": "any"}, "limit": 1},
        )
    direct_client = BookOrbitClient(IT_URL, IT_API_KEY)
    direct = await direct_client.query_books(
        build_book_query(
            filter=all_of(
                any_of(
                    rule("isbn", "isEmpty"),
                    rule("communityRating", "isEmpty", provider="amazon"),
                )
            ),
            size=1,
        )
    )
    await direct_client.close()
    assert result.data.total == direct["total"]


@needs_key
async def test_series_and_smart_scope_counts_match_their_book_lists(live_tools):
    from fastmcp import Client

    async with Client(server.mcp) as c:
        series = (await c.call_tool("list_series", {"limit": 1})).data["series"]
        if series:
            books = (await c.call_tool("get_series_books", {"series_id": series[0]["id"]})).data
            assert books.total == series[0]["book_count"]
        scopes = (await c.call_tool("list_smart_scopes", {})).structured_content["result"]
        if scopes:
            books = (await c.call_tool("get_smart_scope_books", {"scope_id": scopes[0]["id"]})).data
            assert books.total == scopes[0]["book_count"]


@needs_key
async def test_library_statistics_summary_matches_query_total(live_tools):
    from fastmcp import Client

    async with Client(server.mcp) as c:
        stats = (await c.call_tool("get_library_statistics", {"report": "summary"})).data
        search = (await c.call_tool("search_books", {"limit": 1})).data
    assert stats["data"]["totalBooks"] == search.total
