"""Read tools, end to end through FastMCP's in-memory client against respx mocks of
BookOrbit's API."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

import server

BASE = "http://bookorbit.test"
QUERY = f"{BASE}/api/v1/books/query"


def _card(i: int, **extra) -> dict:
    return {
        "id": i,
        "title": f"Book {i}",
        "authors": ["Ann"],
        "readStatus": None,
        "files": [{"id": i, "format": "epub", "role": "primary"}],
        **extra,
    }


def _page(cards: list[dict], total: int, page: int = 0, size: int = 50) -> httpx.Response:
    return httpx.Response(201, json={"items": cards, "total": total, "page": page, "size": size})


def _bodies(route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls]


async def _call(name: str, args: dict | None = None):
    async with Client(server.mcp) as c:
        return await c.call_tool(name, args or {})


# --- search_books -------------------------------------------------------------


async def test_search_books_translates_filters_to_rules(authed):
    authed.get(f"{BASE}/api/v1/collections").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "Kobo"}])
    )
    authed.get(f"{BASE}/api/v1/libraries").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "name": "Ebooks"}])
    )
    route = authed.post(QUERY).mock(return_value=_page([_card(1)], 1))

    result = await _call(
        "search_books",
        {
            "query": "dune",
            "filters": {
                "tags": ["sf", "classic"],
                "tags_mode": "all",
                "genres": ["Fiction"],
                "authors": ["herbert"],
                "collection_ids": [4],
                "library_ids": [1],
                "read_status": "read",
                "missing": ["isbn", "amazonRating"],
                "missing_mode": "any",
            },
            "sort": "addedAt",
            "order": "desc",
            "limit": 10,
        },
    )

    body = _bodies(route)[0]
    assert body["q"] == "dune"
    assert body["sort"] == [{"field": "addedAt", "dir": "desc"}]
    assert body["pagination"] == {"page": 0, "size": 10}
    assert body["filter"] == {
        "type": "group",
        "join": "AND",
        "rules": [
            {"type": "rule", "field": "tag", "operator": "includesAll", "value": ["sf", "classic"]},
            {"type": "rule", "field": "genre", "operator": "includesAny", "value": ["Fiction"]},
            {"type": "rule", "field": "author", "operator": "includesAny", "value": ["herbert"]},
            {"type": "rule", "field": "collection", "operator": "includesAny", "value": ["Kobo"]},
            {"type": "rule", "field": "library", "operator": "includesAny", "value": ["Ebooks"]},
            {"type": "rule", "field": "readStatus", "operator": "includesAny", "value": ["read"]},
            {
                "type": "group",
                "join": "OR",
                "rules": [
                    {"type": "rule", "field": "isbn", "operator": "isEmpty"},
                    {
                        "type": "rule",
                        "field": "communityRating",
                        "operator": "isEmpty",
                        "provider": "amazon",
                    },
                ],
            },
        ],
    }
    assert result.data.total == 1 and result.data.books[0].formats == ["epub"]


async def test_search_books_offset_window_spans_two_pages(authed):
    route = authed.post(QUERY).mock(
        side_effect=[
            _page([_card(i) for i in range(1, 11)], 25),
            _page([_card(i) for i in range(11, 21)], 25),
        ]
    )

    result = await _call("search_books", {"limit": 10, "offset": 5})

    assert [b["pagination"] for b in _bodies(route)] == [
        {"page": 0, "size": 10},
        {"page": 1, "size": 10},
    ]
    assert [b.id for b in result.data.books] == list(range(6, 16))
    assert (result.data.total, result.data.offset) == (25, 5)


@pytest.mark.parametrize(
    ("filters", "match"),
    [
        ({"shelf_ids": [1]}, "Unknown filter key"),
        ({"missing": ["asin"]}, "Unknown missing field"),
        ({"read_status": "READ"}, "Unknown read_status"),
        ({"tags": ["x"], "tags_mode": "most"}, "tags_mode"),
        ({"rules": {"type": "group", "join": "AND", "rules": []}}, "at least one rule"),
    ],
)
async def test_search_books_rejects_bad_filters_before_querying(authed, filters, match):
    route = authed.post(QUERY)
    async with Client(server.mcp) as c:
        result = await c.call_tool("search_books", {"filters": filters}, raise_on_error=False)
    assert result.is_error and match in result.content[0].text
    assert not route.called


async def test_search_books_unknown_collection_id(authed):
    authed.get(f"{BASE}/api/v1/collections").mock(
        return_value=httpx.Response(200, json=[{"id": 4, "name": "Kobo"}])
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "search_books", {"filters": {"collection_ids": [9]}}, raise_on_error=False
        )
    assert result.is_error and "Unknown collection id(s): [9]" in result.content[0].text


async def test_list_books_uses_free_text_search(authed):
    route = authed.post(QUERY).mock(return_value=_page([_card(1)], 1))

    result = await _call("list_books", {"query": "tolkien", "limit": 5})

    body = _bodies(route)[0]
    assert body["q"] == "tolkien" and body["sort"] == [{"field": "relevance", "dir": "desc"}]
    assert result.data[0].id == 1


async def test_books_by_author_uses_author_rule(authed):
    route = authed.post(QUERY).mock(return_value=_page([_card(1)], 1))

    await _call("books_by_author", {"author": "tolkien"})

    body = _bodies(route)[0]
    assert body["filter"]["rules"] == [
        {"type": "rule", "field": "author", "operator": "includesAny", "value": ["tolkien"]}
    ]


# --- single records and lists ---------------------------------------------------


async def test_get_book_can_drop_description(authed):
    authed.get(f"{BASE}/api/v1/books/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "description": "long"})
    )
    result = await _call("get_book", {"book_id": 7, "with_description": False})
    assert result.data == {"id": 7}


async def test_list_collections_trims(authed):
    authed.get(f"{BASE}/api/v1/collections").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "name": "Kobo",
                    "bookCount": 37,
                    "isPublic": False,
                    "isOwner": True,
                    "userId": 1,
                    "syncToKobo": True,
                }
            ],
        )
    )
    result = await _call("list_collections")
    assert result.structured_content["result"] == [
        {
            "id": 1,
            "name": "Kobo",
            "description": None,
            "book_count": 37,
            "is_public": False,
            "is_owner": True,
        }
    ]


async def test_get_collection_books_uses_collection_route_and_order(authed):
    route = authed.post(f"{BASE}/api/v1/collections/4/books/query").mock(
        return_value=_page([_card(1)], 1)
    )
    result = await _call("get_collection_books", {"collection_id": 4})
    assert _bodies(route)[0]["sort"] == [{"field": "collectionOrder", "dir": "asc"}]
    assert result.data.books[0].id == 1


@pytest.mark.parametrize(
    ("kind", "path"),
    [("similar", "recommendations"), ("series", "series-books"), ("author", "author-books")],
)
async def test_get_recommendations_kinds(authed, kind, path):
    authed.get(f"{BASE}/api/v1/books/3/{path}").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": 1, "title": "A", "authors": ["Ann"], "readStatus": {"status": "read"}},
                {"id": 2, "title": "B", "authors": [], "readStatus": None, "seriesIndex": "2"},
            ],
        )
    )
    result = await _call("get_recommendations", {"book_id": 3, "kind": kind, "limit": 5})
    assert result.structured_content["result"] == [
        {"id": 1, "title": "A", "authors": ["Ann"], "readStatus": "read"},
        {"id": 2, "title": "B", "authors": [], "readStatus": None, "seriesIndex": "2"},
    ]


async def test_list_authors_pages_from_zero_with_query(authed):
    route = authed.get(f"{BASE}/api/v1/authors").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": 3, "name": "Ann", "bookCount": 2}], "total": 1}
        )
    )
    result = await _call("list_authors", {"query": "an", "limit": 20})
    assert dict(route.calls.last.request.url.params) == {"page": "0", "size": "20", "q": "an"}
    assert result.data == {"total": 1, "authors": [{"id": 3, "name": "Ann", "book_count": 2}]}


async def test_list_libraries_reports_folders_as_paths(authed):
    authed.get(f"{BASE}/api/v1/libraries").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "name": "Ebooks",
                    "bookCount": 433,
                    "watch": True,
                    "folders": [{"id": 9, "path": "/books", "createdAt": "x"}],
                    "allowedFormats": ["epub"],
                    "fileWriteEnabled": True,
                }
            ],
        )
    )
    result = await _call("list_libraries")
    assert result.structured_content["result"] == [
        {
            "id": 1,
            "name": "Ebooks",
            "book_count": 433,
            "watch": True,
            "paths": [{"id": 9, "path": "/books"}],
            "allowed_formats": ["epub"],
        }
    ]


# --- stats and export ---------------------------------------------------------------


async def test_get_reading_stats_counts_statuses_and_ratings(authed):
    def respond(request):
        flt = json.loads(request.content).get("filter")
        if flt is None:
            return _page([], 20)
        r = flt["rules"][0]
        if r["field"] == "readStatus":
            return _page([], {"unread": 12, "reading": 3, "read": 5}.get(r["value"][0], 0))
        return _page([], {4: 2, 5: 1}.get(r["value"], 0))

    authed.post(QUERY).mock(side_effect=respond)
    authed.get(f"{BASE}/api/v1/user-statistics/summary").mock(
        return_value=httpx.Response(200, json={"trackedBooks": 8})
    )

    d = (await _call("get_reading_stats")).data

    assert d["total"] == 20 and sum(d["by_status"].values()) == 20
    assert d["by_status"]["reading"] == 3
    assert d["rated_count"] == 3 and d["avg_rating"] == 4.33
    assert d["reading"] == {"trackedBooks": 8}


async def test_export_library_csv_with_fields_and_collection_scope(authed):
    route = authed.post(f"{BASE}/api/v1/collections/4/books/query").mock(
        return_value=_page(
            [_card(1, genres=["SF"], readStatus={"status": "read"}), _card(2, genres=[])], 2
        )
    )

    d = (
        await _call(
            "export_library",
            {"format": "csv", "collection_id": 4, "fields": ["id", "genres", "readStatus"]},
        )
    ).data

    assert d["content"].splitlines() == ["id,genres,readStatus", "1,SF,read", "2,,"]
    assert (d["total"], d["returned"]) == (2, 2)
    assert _bodies(route)[0]["pagination"] == {"page": 0, "size": 200}


async def test_export_library_rejects_unknown_field(authed):
    async with Client(server.mcp) as c:
        result = await c.call_tool("export_library", {"fields": ["asin"]}, raise_on_error=False)
    assert result.is_error and "Unknown export field" in result.content[0].text


# --- metadata stream ------------------------------------------------------------------

_PROVIDERS = [{"key": "google"}, {"key": "goodreads"}, {"key": "amazon"}]


def _sse(*events: tuple[str | None, dict]) -> httpx.Response:
    chunks = []
    for name, payload in events:
        lines = ([f"event: {name}"] if name else []) + [f"data: {json.dumps(payload)}"]
        chunks.append("\n".join(lines) + "\n\n")
    return httpx.Response(
        200, text=": keepalive\n\n" + "".join(chunks), headers={"content-type": "text/event-stream"}
    )


async def test_fetch_metadata_candidates_reports_each_provider(authed):
    authed.get(f"{BASE}/api/v1/metadata-fetch/providers").mock(
        return_value=httpx.Response(200, json=_PROVIDERS)
    )
    stream = authed.get(f"{BASE}/api/v1/metadata-fetch/stream").mock(
        return_value=_sse(
            (None, {"provider": "google", "providerId": "g1", "title": "Dune"}),
            (None, {"provider": "google", "providerId": "g2", "title": "Dune 2"}),
            ("provider-status", {"provider": "amazon", "outcome": "throttled"}),
        )
    )

    d = (
        await _call(
            "fetch_metadata_candidates",
            {"book_id": 7, "providers": ["google", "goodreads", "amazon", "kobo"]},
        )
    ).data

    params = dict(stream.calls.last.request.url.params)
    assert params == {"bookId": "7", "providers": "google,goodreads,amazon"}  # kobo not sent
    assert stream.calls.last.request.headers["X-Api-Key"].startswith("bok_")
    assert [(s["provider"], s["status"], s["count"]) for s in d["provider_status"]] == [
        ("google", "ok", 2),
        ("goodreads", "empty", 0),
        ("amazon", "throttled", 0),
        ("kobo", "disabled", 0),
    ]
    assert d["count"] == 2 and d["candidates"][1]["providerId"] == "g2"


async def test_isbn_lookup_defaults_to_enabled_providers(authed):
    authed.get(f"{BASE}/api/v1/metadata-fetch/providers").mock(
        return_value=httpx.Response(200, json=_PROVIDERS)
    )
    stream = authed.get(f"{BASE}/api/v1/metadata-fetch/stream").mock(
        return_value=_sse((None, {"provider": "goodreads", "isbn13": "9780441013593"}))
    )

    d = (await _call("isbn_lookup", {"isbn": "9780441013593"})).data

    assert dict(stream.calls.last.request.url.params) == {
        "isbn": "9780441013593",
        "providers": "google,goodreads,amazon",
    }
    assert d["isbn"] == "9780441013593" and d["count"] == 1


async def test_metadata_stream_error_is_reported(authed):
    authed.get(f"{BASE}/api/v1/metadata-fetch/providers").mock(
        return_value=httpx.Response(200, json=_PROVIDERS)
    )
    authed.get(f"{BASE}/api/v1/metadata-fetch/stream").mock(
        return_value=httpx.Response(403, json={"message": "Missing permission: x"})
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool("isbn_lookup", {"isbn": "1"}, raise_on_error=False)
    assert result.is_error and "Missing permission: x" in result.content[0].text


# --- duplicates ------------------------------------------------------------------------

_SCANS = f"{BASE}/api/v1/book-duplicates/scans"


def _scan(status: str, **extra) -> dict:
    return {
        "id": 5,
        "status": status,
        "libraryIds": [1],
        "similarityPercent": 90,
        "progressPercent": 100 if status == "completed" else 40,
        "totalGroups": 1,
        **extra,
    }


def _group(gid: int) -> dict:
    return {
        "id": gid,
        "kind": "duplicate",
        "reasons": ["isbn"],
        "reclaimableBytes": 10,
        "books": [
            {
                "id": 1,
                "title": "Dune",
                "authors": ["Frank Herbert"],
                "isbn13": "1",
                "files": [{"format": "epub"}],
                "libraryName": "Ebooks",
            },
            {
                "id": 2,
                "title": "Dune",
                "authors": ["Frank Herbert"],
                "isbn13": "2",
                "files": [{"format": "pdf"}],
                "libraryName": "Ebooks",
            },
        ],
    }


@pytest.fixture
def no_wait(monkeypatch):
    monkeypatch.setattr(server, "DUP_POLL_INTERVAL", 0)


async def test_find_duplicates_starts_scan_polls_and_reads_groups(authed, no_wait):
    authed.get(f"{_SCANS}/active").mock(return_value=httpx.Response(200))
    start = authed.post(_SCANS).mock(return_value=httpx.Response(201, json=_scan("queued")))
    authed.get(f"{_SCANS}/5").mock(
        side_effect=[
            httpx.Response(200, json=_scan("running")),
            httpx.Response(200, json=_scan("completed")),
        ]
    )
    groups = authed.get(f"{_SCANS}/5/groups").mock(
        return_value=httpx.Response(
            200, json={"groups": [_group(1)], "total": 1, "page": 1, "pageSize": 50}
        )
    )

    d = (await _call("find_duplicates", {"library_id": 1, "reason": "isbn"})).data

    assert json.loads(start.calls.last.request.content) == {
        "similarityPercent": 90,
        "libraryId": 1,
    }
    assert dict(groups.calls.last.request.url.params) == {
        "page": "1",
        "pageSize": "50",
        "kind": "duplicate",
        "reason": "isbn",
    }
    assert d["status"] == "completed" and d["reused_active_scan"] is False
    g = d["groups"][0]
    assert g["distinct_isbns"] is True and g["books"][1]["formats"] == ["pdf"]


async def test_find_duplicates_reuses_active_scan(authed, no_wait):
    authed.get(f"{_SCANS}/active").mock(return_value=httpx.Response(200, json=_scan("completed")))
    start = authed.post(_SCANS)
    authed.get(f"{_SCANS}/5/groups").mock(
        return_value=httpx.Response(200, json={"groups": [], "total": 0})
    )

    d = (await _call("find_duplicates")).data

    assert not start.called and d["reused_active_scan"] is True and d["groups"] == []


async def test_find_duplicates_returns_scan_id_when_still_running(authed):
    authed.get(f"{_SCANS}/active").mock(return_value=httpx.Response(200))
    authed.post(_SCANS).mock(return_value=httpx.Response(201, json=_scan("running")))

    d = (await _call("find_duplicates", {"wait_seconds": 0})).data

    assert d["status"] == "running" and d["scan_id"] == 5
    assert "scan_id=5" in d["message"]


async def test_find_duplicates_failed_scan_raises(authed, no_wait):
    authed.get(f"{_SCANS}/5").mock(
        return_value=httpx.Response(200, json=_scan("failed", errorCode="SCAN_ERROR"))
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool("find_duplicates", {"scan_id": 5}, raise_on_error=False)
    assert result.is_error and "failed (SCAN_ERROR)" in result.content[0].text


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"similarity_percent": 60}, "between 70 and 100"),
        ({"kind": "other_format", "reason": "isbn"}, "does not apply"),
    ],
)
async def test_find_duplicates_validates_arguments(authed, args, match):
    async with Client(server.mcp) as c:
        result = await c.call_tool("find_duplicates", args, raise_on_error=False)
    assert result.is_error and match in result.content[0].text
