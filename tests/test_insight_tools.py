"""Series, smart scopes, statistics, reading history, annotations and metadata
refresh, end to end through FastMCP's in-memory client against respx mocks."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

import server

BASE = "http://bookorbit.test"


async def _call(name: str, args: dict | None = None):
    async with Client(server.mcp) as c:
        return await c.call_tool(name, args or {})


async def _error(name: str, args: dict) -> str:
    async with Client(server.mcp) as c:
        result = await c.call_tool(name, args, raise_on_error=False)
    assert result.is_error
    return result.content[0].text


def _params(route) -> dict:
    return dict(route.calls.last.request.url.params.multi_items())


def _card(i: int) -> dict:
    return {"id": i, "title": f"Book {i}", "authors": [], "readStatus": None, "files": []}


# --- series ------------------------------------------------------------------------------


async def test_list_series_trims_and_reports_next_book(authed):
    route = authed.get(f"{BASE}/api/v1/series").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": 7,
                        "name": "Dune",
                        "authors": ["Frank Herbert"],
                        "bookCount": 3,
                        "readCount": 1,
                        "readingCount": 1,
                        "expectedBookCount": 6,
                        "gaps": ["4"],
                        "nextBookId": 12,
                        "nextTitle": "Dune Messiah",
                        "nextIndex": "2",
                        "volumes": [{"bookId": 1}],
                        "facets": {},
                    },
                    {"id": 8, "name": "Done", "nextBookId": None},
                ],
                "total": 2,
            },
        )
    )

    d = (await _call("list_series", {"query": "du", "status": "has_gaps", "limit": 10})).data

    assert _params(route) == {
        "page": "0",
        "size": "10",
        "sort": "name",
        "order": "asc",
        "q": "du",
        "completionStatus": "has_gaps",
    }
    assert d["total"] == 2
    assert d["series"][0] == {
        "id": 7,
        "name": "Dune",
        "authors": ["Frank Herbert"],
        "book_count": 3,
        "read_count": 1,
        "reading_count": 1,
        "expected_book_count": 6,
        "gaps": ["4"],
        "next": {"book_id": 12, "title": "Dune Messiah", "index": "2"},
    }
    assert "next" not in d["series"][1]


async def test_get_series_books(authed):
    route = authed.get(f"{BASE}/api/v1/series/7/books").mock(
        return_value=httpx.Response(200, json={"items": [_card(1), _card(2)], "total": 2})
    )
    d = (await _call("get_series_books", {"series_id": 7})).data
    assert _params(route) == {"page": "0", "size": "50", "sort": "seriesIndex", "order": "asc"}
    assert [b.id for b in d.books] == [1, 2] and d.total == 2


async def test_search_books_series_filter(authed):
    route = authed.post(f"{BASE}/api/v1/books/query").mock(
        return_value=httpx.Response(201, json={"items": [], "total": 0})
    )
    await _call("search_books", {"filters": {"series": ["Dune"]}})
    assert json.loads(route.calls.last.request.content)["filter"]["rules"] == [
        {"type": "rule", "field": "series", "operator": "includesAny", "value": ["Dune"]}
    ]


# --- smart scopes ---------------------------------------------------------------------------


async def test_list_smart_scopes_includes_filter(authed):
    flt = {"type": "group", "join": "AND", "rules": []}
    authed.get(f"{BASE}/api/v1/smart-scopes").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "name": "Reading",
                    "bookCount": 5,
                    "isPublic": True,
                    "isOwner": True,
                    "filter": flt,
                    "syncToKobo": False,
                }
            ],
        )
    )
    d = (await _call("list_smart_scopes")).structured_content["result"]
    assert d == [
        {
            "id": 1,
            "name": "Reading",
            "book_count": 5,
            "is_public": True,
            "is_owner": True,
            "filter": flt,
        }
    ]


@pytest.mark.parametrize(
    ("args", "sort"),
    [({}, None), ({"sort": "addedAt", "order": "desc"}, [{"field": "addedAt", "dir": "desc"}])],
)
async def test_get_smart_scope_books_uses_scope_route(authed, args, sort):
    route = authed.post(f"{BASE}/api/v1/smart-scopes/3/books/query").mock(
        return_value=httpx.Response(201, json={"items": [_card(1)], "total": 1})
    )
    d = (await _call("get_smart_scope_books", {"scope_id": 3, **args})).data
    body = json.loads(route.calls.last.request.content)
    assert body.get("sort") == sort  # no sort sent: the scope's default order applies
    assert d.books[0].id == 1


# --- statistics ---------------------------------------------------------------------------------


async def test_library_statistics_params_and_envelope(authed):
    route = authed.get(f"{BASE}/api/v1/statistics/books-added-over-time").mock(
        return_value=httpx.Response(200, json={"items": []})
    )
    d = (
        await _call(
            "get_library_statistics",
            {
                "report": "books-added-over-time",
                "library_ids": [1, 2],
                "granularity": "yearly",
                "range": "all-time",
            },
        )
    ).data
    assert route.calls.last.request.url.params.get_list("libraryIds") == ["1", "2"]
    assert _params(route)["granularity"] == "yearly"
    assert d == {"report": "books-added-over-time", "data": {"items": []}}


async def test_library_statistics_granularity_only_for_over_time(authed):
    assert "only apply" in await _error(
        "get_library_statistics", {"report": "summary", "granularity": "monthly"}
    )


async def test_reading_statistics_list_report_is_structured(authed):
    route = authed.get(f"{BASE}/api/v1/user-statistics/peak-hours").mock(
        return_value=httpx.Response(200, json=[{"hour": 0, "readingSeconds": 10}])
    )
    result = await _call("get_reading_statistics", {"report": "peak-hours", "days": 30})
    assert _params(route) == {"days": "30"}
    assert result.data == {"report": "peak-hours", "data": [{"hour": 0, "readingSeconds": 10}]}


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"report": "summary", "days": 30}, "does not apply"),
        ({"report": "peak-hours", "days": 0}, "between 1 and 3650"),
    ],
)
async def test_reading_statistics_validates_days(authed, args, match):
    assert match in await _error("get_reading_statistics", args)


async def test_reading_statistics_offers_no_key_forbidden_reports():
    reports = set(server.ReadingReport.__args__)
    assert not reports & {
        "activity-overview",
        "activity-calendar",
        "session-patterns",
        "genre-time",
    }


# --- reading history and annotations ------------------------------------------------------------


async def test_reading_history_for_a_book(authed):
    history = authed.get(f"{BASE}/api/v1/reading-history").mock(
        return_value=httpx.Response(
            200, json={"items": [{"id": 1, "outcome": "completed"}], "total": 1}
        )
    )
    summary = authed.get(f"{BASE}/api/v1/reading-history/summary").mock(
        return_value=httpx.Response(200, json={"reads": 1})
    )
    authed.get(f"{BASE}/api/v1/books/5/sessions").mock(
        return_value=httpx.Response(200, json={"items": [], "stats": {"totalSessions": 3}})
    )

    d = (
        await _call(
            "get_reading_history",
            {"book_id": 5, "year": 2026, "quarter": 2, "outcomes": ["completed", "skimmed"]},
        )
    ).data

    assert history.calls.last.request.url.params.get_list("outcomes") == ["completed", "skimmed"]
    assert {k: v for k, v in _params(history).items() if k != "outcomes"} == {
        "year": "2026",
        "quarter": "2",
        "sort": "newest",
        "offset": "0",
        "limit": "50",
        "bookId": "5",
    }
    assert _params(summary) == {"year": "2026", "quarter": "2"}
    assert d == {
        "total": 1,
        "reads": [{"id": 1, "outcome": "completed"}],
        "summary": {"reads": 1},
        "sessions": {"totalSessions": 3},
    }


async def test_reading_history_quarter_needs_year(authed):
    assert "quarter needs a year" in await _error("get_reading_history", {"quarter": 1})


async def test_get_annotations_trims(authed):
    authed.get(f"{BASE}/api/v1/books/9/annotations").mock(
        return_value=httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "text": "quote",
                    "note": "mine",
                    "chapterTitle": "One",
                    "color": "yellow",
                    "highlightedAt": "2026-10-01",
                    "starredAt": None,
                    "cfi": "epubcfi(...)",
                }
            ],
        )
    )
    authed.get(f"{BASE}/api/v1/books/9/bookmarks").mock(return_value=httpx.Response(200, json=[]))
    d = (await _call("get_annotations", {"book_id": 9})).data
    assert d == {
        "highlights": [
            {
                "id": 1,
                "text": "quote",
                "note": "mine",
                "chapter": "One",
                "color": "yellow",
                "highlighted_at": "2026-10-01",
                "starred": False,
            }
        ],
        "bookmarks": [],
    }


# --- metadata refresh ---------------------------------------------------------------


async def test_refresh_metadata_preview_writes_nothing(authed):
    preview = authed.post(f"{BASE}/api/v1/books/5/refresh-metadata").mock(
        return_value=httpx.Response(201, json={"metadata": {"title": "X"}, "diagnostics": {}})
    )
    authed.post(f"{BASE}/api/v1/books/6/refresh-metadata").mock(
        return_value=httpx.Response(
            403, json={"message": "Missing permission: library_edit_metadata"}
        )
    )
    bulk = authed.post(f"{BASE}/api/v1/books/bulk-refresh-metadata")

    d = (await _call("refresh_metadata", {"book_ids": [5, 6]})).data

    assert _params(preview) == {"preview": "true"} and not bulk.called
    assert d["preview"] is True
    assert d["previews"][0] == {"book_id": 5, "metadata": {"title": "X"}, "diagnostics": {}}
    assert "Missing permission" in d["previews"][1]["error"]


async def test_refresh_metadata_preview_is_capped(authed):
    assert "at most 10" in await _error("refresh_metadata", {"book_ids": list(range(1, 12))})


async def test_refresh_metadata_apply_reads_the_progress_stream(authed):
    events = [
        {"bookId": 5, "success": True},
        {"bookId": 6, "success": False, "error": "provider failed"},
        {"done": True, "processed": 2, "failed": 1},
    ]
    stream = authed.post(f"{BASE}/api/v1/books/bulk-refresh-metadata").mock(
        return_value=httpx.Response(
            200,
            text="".join(f"data: {json.dumps(e)}\n\n" for e in events),
            headers={"content-type": "text/event-stream"},
        )
    )

    d = (
        await _call(
            "refresh_metadata", {"book_ids": [5, 6], "preview": False, "ignore_cache": True}
        )
    ).data

    assert json.loads(stream.calls.last.request.content) == {"bookIds": [5, 6], "ignoreCache": True}
    assert d == {
        "preview": False,
        "refreshed": [5],
        "failed": [{"book_id": 6, "error": "provider failed"}],
        "processed": 2,
        "completed": True,
    }


async def test_refresh_metadata_apply_error(authed):
    authed.post(f"{BASE}/api/v1/books/bulk-refresh-metadata").mock(
        return_value=httpx.Response(
            403, json={"message": "Missing permission: library_edit_metadata"}
        )
    )
    assert "Missing permission" in await _error(
        "refresh_metadata", {"book_ids": [5], "preview": False}
    )
