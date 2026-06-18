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


def test_matches_filters_missing_any_mode_and_identifier_fields():
    book = {"metadata": {"asin": "B01", "goodreadsId": ""}}
    # "all" (default): every field must be empty — asin is present, so excluded.
    assert not server._matches_filters(book, {"missing": ["asin", "goodreadsId"]})
    # "any": at least one empty — goodreadsId is empty, so included.
    assert server._matches_filters(
        book, {"missing": ["asin", "goodreadsId"], "missing_mode": "any"}
    )


def test_normalize_goodreads_id_strips_slug_keeps_numeric():
    assert server._normalize_goodreads_id("23463279-designing-data-intensive") == "23463279"
    assert server._normalize_goodreads_id("171712768") == "171712768"
    assert server._normalize_goodreads_id("  42  ") == "42"
    assert server._normalize_goodreads_id(None) is None


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


async def test_fetch_metadata_candidates_defaults_query_and_reports_status(authed):
    # No isbn/title/author passed -> defaulted from the book's own stored metadata.
    authed.get(f"{BASE}/api/v1/books/7").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": 7,
                "title": "Stored Title",
                "metadata": {"isbn13": "111", "authors": ["Ann"]},
            },
        )
    )
    authed.get(f"{BASE}/api/v1/settings").mock(
        return_value=httpx.Response(
            200, json={"metadataProviderSettings": {"google": {"enabled": True}}}
        )
    )
    sse = (
        'data: {"title": "Cand A", "provider": "Google"}\n\n'
        'data: {"title": "Cand B", "provider": "Google"}\n\n'
    )
    route = authed.post(f"{BASE}/api/v1/books/7/metadata/prospective").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "fetch_metadata_candidates", {"book_id": 7, "providers": ["Google"]}
        )

    d = result.data
    assert d["count"] == 2
    assert {c["title"] for c in d["candidates"]} == {"Cand A", "Cand B"}
    # P1: search terms came from stored metadata, and were sent to the backend.
    assert d["query"] == {"isbn": "111", "title": "Stored Title", "author": "Ann"}
    body = json.loads(route.calls.last.request.content)
    assert (body["isbn"], body["title"], body["author"]) == ("111", "Stored Title", "Ann")
    # P0: per-provider status surfaces the result.
    assert d["provider_status"] == [{"provider": "Google", "status": "ok", "count": 2}]


async def test_fetch_metadata_candidates_flags_empty_and_disabled(authed):
    authed.get(f"{BASE}/api/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "metadataProviderSettings": {
                    "amazon": {"enabled": True, "cookie": ""},
                    "goodReads": {"enabled": False},
                }
            },
        )
    )
    # Both providers ran but the stream carried no candidates for either.
    authed.post(f"{BASE}/api/v1/books/9/metadata/prospective").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text="")
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "fetch_metadata_candidates",
            {"book_id": 9, "providers": ["Amazon", "GoodReads"], "title": "x"},
        )

    statuses = {s["provider"]: s for s in result.data["provider_status"]}
    assert statuses["GoodReads"]["status"] == "disabled"
    assert statuses["Amazon"]["status"] == "empty"
    assert "cookie" in statuses["Amazon"]["reason"]


async def test_export_library_fields_projection_fetches_full(authed):
    books = [
        {"id": 1, "title": "T", "metadata": {"asin": "B01", "isbn13": "111", "authors": ["Ann"]}}
    ]
    route = authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "export_library", {"format": "json", "fields": ["id", "asin", "authors"]}
        )

    d = result.data
    assert d["fields"] == ["id", "asin", "authors"]
    assert json.loads(d["content"]) == [{"id": 1, "asin": "B01", "authors": ["Ann"]}]
    # asin is stripped from the plain list response -> withDescription must be requested.
    assert route.calls.last.request.url.params["withDescription"] == "true"


async def test_bulk_set_read_status_groups_by_status(authed):
    route = authed.post(f"{BASE}/api/v1/books/status").mock(
        return_value=httpx.Response(200, json=[])
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "bulk_set_read_status",
            {
                "items": [
                    {"book_id": 1, "status": "READ"},
                    {"book_id": 2, "status": "READ"},
                    {"book_id": 3, "status": "READING"},
                    {"book_id": 4, "status": "NONSENSE"},
                ]
            },
        )

    d = result.data
    assert sorted(d["updated"]) == [1, 2, 3]
    assert d["by_status"] == {"READ": 2, "READING": 1}
    assert d["failed"][0]["book_id"] == 4
    assert route.call_count == 2  # one call per distinct valid status, not per book


async def test_find_duplicates_wraps_native_endpoint(authed):
    authed.get(f"{BASE}/api/v1/libraries").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    groups = [
        {
            "matchReason": "TITLE_AUTHOR",
            "suggestedTargetBookId": 95,
            "books": [
                {"id": 95, "title": "CKA", "metadata": {"isbn13": "111", "authors": ["A"]}},
                {"id": 159, "title": "CKA", "metadata": {"isbn13": "222", "authors": ["A"]}},
            ],
        }
    ]
    route = authed.post(f"{BASE}/api/v1/books/duplicates").mock(
        return_value=httpx.Response(200, json=groups)
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool("find_duplicates", {"preset": "balanced"})

    d = result.data
    assert d["libraries_scanned"] == 1 and d["group_count"] == 1
    g = d["groups"][0]
    assert g["match_reason"] == "TITLE_AUTHOR"
    assert g["suggested_target_book_id"] == 95
    assert g["distinct_isbns"] is True  # different ISBNs -> likely separate editions
    body = json.loads(route.calls.last.request.content)
    assert body["matchByIsbn"] and body["matchByExternalId"] and body["matchByTitleAuthor"]
    assert body["matchByDirectory"] is False and body["matchByFilename"] is False


async def test_fetch_metadata_candidates_flags_disabled_provider_fields(authed):
    # GoodReads runs and returns a candidate, but the Goodreads Rating field is toggled
    # off in Settings > Metadata 2 -> surfaced so the caller knows why ratings never land.
    authed.get(f"{BASE}/api/v1/settings").mock(
        return_value=httpx.Response(
            200,
            json={
                "metadataProviderSettings": {"goodReads": {"enabled": True}},
                "metadataProviderSpecificFields": {"goodreadsRating": False, "goodreadsId": True},
            },
        )
    )
    sse = 'data: {"title": "DDIA", "provider": "GoodReads", "goodreadsId": "23463279"}\n\n'
    authed.post(f"{BASE}/api/v1/books/21/metadata/prospective").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/event-stream"}, text=sse)
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "fetch_metadata_candidates",
            {"book_id": 21, "providers": ["GoodReads"], "title": "DDIA"},
        )

    gr = result.data["provider_status"][0]
    assert gr["status"] == "ok" and gr["count"] == 1
    assert gr["disabled_fields"] == ["goodreadsRating"]


async def test_export_library_paginates_and_reports_total(authed):
    books = [{"id": i, "title": f"B{i}", "metadata": {}} for i in range(5)]
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "export_library", {"format": "json", "fields": ["id"], "offset": 1, "limit": 2}
        )

    d = result.data
    assert (d["total"], d["offset"], d["limit"], d["returned"]) == (5, 1, 2, 2)
    assert json.loads(d["content"]) == [{"id": 1}, {"id": 2}]


async def test_normalize_goodreads_ids_dry_run_then_apply(authed):
    books = [
        {
            "id": 21,
            "title": "DDIA",
            "metadata": {"goodreadsId": "23463279-designing-data-intensive"},
        },
        {"id": 22, "title": "Clean", "metadata": {"goodreadsId": "171712768"}},
    ]
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))
    put = authed.put(f"{BASE}/api/v1/books/21/metadata").mock(
        return_value=httpx.Response(200, json={})
    )
    async with Client(server.mcp) as c:
        dry = await c.call_tool("normalize_goodreads_ids", {})
        # Only the slug record is flagged; the already-numeric one is left alone.
        assert dry.data["dry_run"] is True
        assert dry.data["to_change"] == [
            {"book_id": 21, "from": "23463279-designing-data-intensive", "to": "23463279"}
        ]
        applied = await c.call_tool("normalize_goodreads_ids", {"dry_run": False})

    assert applied.data["changed"][0]["book_id"] == 21
    body = json.loads(put.calls.last.request.content)
    assert body["metadata"] == {"goodreadsId": "23463279"}  # only the one field is touched
