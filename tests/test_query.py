"""Book queries: building/validating BookQuery bodies, the three query routes,
paging, and BookCard summaries."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

import server
from server import (
    BookOrbitClient,
    BookOrbitError,
    all_of,
    any_of,
    build_book_query,
    rule,
    sort_by,
    validate_filter,
    validate_sort,
)

BASE = "http://bookorbit.test"
KEY = "bok_" + "q" * 49


# --- building ---------------------------------------------------------------


def test_builders_produce_bookorbit_shapes():
    f = any_of(rule("isbn", "isEmpty"), rule("genre", "includesAny", ["Fantasy"]))
    assert f == {
        "type": "group",
        "join": "OR",
        "rules": [
            {"type": "rule", "field": "isbn", "operator": "isEmpty"},
            {"type": "rule", "field": "genre", "operator": "includesAny", "value": ["Fantasy"]},
        ],
    }
    assert all_of(rule("title", "contains", "x"))["join"] == "AND"
    assert sort_by("title") == {"field": "title", "dir": "asc"}
    assert rule("publishedYear", "between", 1990, 2000)["valueTo"] == 2000


def test_build_book_query_defaults_to_first_page():
    assert build_book_query() == {"pagination": {"page": 0, "size": 50}}


def test_build_book_query_includes_filter_sort_and_q():
    f = all_of(rule("readStatus", "includesAny", ["read"]))
    query = build_book_query(filter=f, sort=[sort_by("addedAt", "desc")], q="dune", size=10)
    assert query == {
        "pagination": {"page": 0, "size": 10},
        "filter": f,
        "sort": [{"field": "addedAt", "dir": "desc"}],
        "q": "dune",
    }


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"size": 0}, "Page size"),
        ({"size": 201}, "Page size"),
        ({"page": -1}, "Page must be"),
        ({"page": 5001, "size": 200}, "Too deep"),
        ({"q": "x" * 201}, "200 characters"),
    ],
)
def test_build_book_query_rejects_bad_paging_and_q(kwargs, match):
    with pytest.raises(BookOrbitError, match=match):
        build_book_query(**kwargs)


# --- filter validation ------------------------------------------------------


def test_valid_filters_pass():
    validate_filter(
        all_of(
            rule("author", "includesAll", ["Ann", "Bob"]),
            rule("publishedDate", "between", "2000-01-01", "2010-12-31"),
            rule("addedAt", "moreThanAgo", 30),
            rule("communityRating", "gte", 4, provider="goodreads"),
            rule("custom:12", "eq", "x"),
            any_of(rule("readProgress", "isUnread"), rule("rating", "gte", 4)),
        )
    )


@pytest.mark.parametrize(
    ("node", "match"),
    [
        (all_of(rule("shelf", "eq", "x")), "unknown filter field 'shelf'"),
        (all_of(rule("title", "includesAny", ["x"])), "not valid for 'title'"),
        (all_of(rule("genre", "includesAny", "Fantasy")), "needs a list value"),
        (all_of(rule("tag", "includesAny", [])), "1 to 20 items"),
        (all_of(rule("tag", "includesAny", ["x"] * 21)), "1 to 20 items"),
        (all_of(rule("tag", "includesAny", ["x", 1])), "all non-empty strings"),
        (all_of(rule("tag", "includesAny", ["x", ""])), "all non-empty strings"),
        (all_of(rule("title", "eq", True)), "string, number, or list"),
        (all_of(rule("addedAt", "before", "last week")), "needs a date value"),
        (all_of(rule("addedAt", "between", "2020-01-01")), "date valueTo"),
        (all_of(rule("finishedAt", "moreThanAgo", -1)), "non-negative day count"),
        (all_of(rule("rating", "gte", 4, provider="goodreads")), "community rating"),
        (all_of(rule("custom:3", "eq", ["x"])), "do not accept list values"),
        (all_of({"field": "title", "operator": "isEmpty"}), "missing 'type'"),
        (all_of({**rule("title", "isEmpty"), "direction": "x"}), "unknown rule keys"),
        ({"type": "group", "join": "AND", "rules": []}, "at least one rule"),
        ({"type": "group", "join": "XOR", "rules": [rule("title", "isEmpty")]}, "join must"),
        (rule("title", "isEmpty"), "expected a group"),
    ],
)
def test_invalid_filters_explain_the_problem(node, match):
    with pytest.raises(BookOrbitError, match=match):
        validate_filter(node)


def test_filter_depth_limit_counts_top_level_group():
    leaf = rule("title", "isEmpty")
    node = all_of(leaf)
    for _ in range(4):
        node = all_of(node)
    validate_filter(node)  # 5 levels: OK
    with pytest.raises(BookOrbitError, match="at most 5 deep"):
        validate_filter(all_of(node))


def test_invalid_filter_error_names_the_location():
    with pytest.raises(BookOrbitError, match=r"filter\.rules\[1\]\.rules\[0\]"):
        validate_filter(all_of(rule("title", "isEmpty"), any_of(rule("nope", "eq", 1))))


# --- sort validation --------------------------------------------------------


@pytest.mark.parametrize(
    ("sort", "match"),
    [
        ([{"field": "title", "direction": "asc"}], "expected"),
        ([sort_by("shelf")], "unknown sort field"),
        ([{"field": "title", "dir": "up"}], "dir must be"),
        ([sort_by("collectionOrder")], "inside one collection"),
        ([sort_by("title")] * 6, "At most 5"),
    ],
)
def test_invalid_sorts_are_rejected(sort, match):
    with pytest.raises(BookOrbitError, match=match):
        validate_sort(sort)


def test_collection_order_allowed_in_collection_scope():
    validate_sort([sort_by("collectionOrder"), sort_by("custom:7", "desc")], collection_scoped=True)


# --- routes -----------------------------------------------------------------


def _page(items, total, page=0, size=50):
    return httpx.Response(201, json={"items": items, "total": total, "page": page, "size": size})


@pytest.mark.parametrize(
    ("scope", "path"),
    [
        ({}, "/api/v1/books/query"),
        ({"library_id": 3}, "/api/v1/libraries/3/books"),
        ({"collection_id": 4}, "/api/v1/collections/4/books/query"),
    ],
)
async def test_query_books_routes_by_scope(respx_mock, scope, path):
    route = respx_mock.post(f"{BASE}{path}").mock(return_value=_page([{"id": 1}], 1))
    client = BookOrbitClient(BASE, KEY)

    result = await client.query_books(build_book_query(size=5), **scope)

    assert result["total"] == 1  # 201 is a success
    assert json.loads(route.calls.last.request.content) == {"pagination": {"page": 0, "size": 5}}


async def test_query_books_rejects_two_scopes():
    client = BookOrbitClient(BASE, KEY)
    with pytest.raises(BookOrbitError, match="not both"):
        await client.query_books({}, library_id=1, collection_id=2)


# --- paging -----------------------------------------------------------------


async def test_iter_book_pages_walks_until_total(respx_mock):
    route = respx_mock.post(f"{BASE}/api/v1/books/query").mock(
        side_effect=[
            _page([{"id": 1}, {"id": 2}], 5),
            _page([{"id": 3}, {"id": 4}], 5),
            _page([{"id": 5}], 5),
        ]
    )
    client = BookOrbitClient(BASE, KEY)
    ctx = AsyncMock()

    pages = [p async for p in client.iter_book_pages(max_books=100, page_size=2, ctx=ctx)]

    assert [b["id"] for p in pages for b in p["items"]] == [1, 2, 3, 4, 5]
    sent = [json.loads(c.request.content)["pagination"] for c in route.calls]
    assert sent == [{"page": 0, "size": 2}, {"page": 1, "size": 2}, {"page": 2, "size": 2}]
    assert ctx.report_progress.await_args_list[-1].kwargs == {"progress": 5, "total": 5}


async def test_iter_book_pages_stops_at_max_books(respx_mock):
    route = respx_mock.post(f"{BASE}/api/v1/books/query").mock(
        side_effect=[_page([{"id": 1}, {"id": 2}, {"id": 3}], 1000)] * 2
    )
    client = BookOrbitClient(BASE, KEY)

    pages = [p async for p in client.iter_book_pages(max_books=4, page_size=3)]

    assert [b["id"] for p in pages for b in p["items"]] == [1, 2, 3, 1]  # last page trimmed
    assert pages[0]["total"] == 1000  # caller can tell it was truncated
    assert route.call_count == 2


async def test_iter_book_pages_page_size_never_exceeds_max_books(respx_mock):
    route = respx_mock.post(f"{BASE}/api/v1/books/query").mock(return_value=_page([{"id": 1}], 1))
    client = BookOrbitClient(BASE, KEY)

    [p async for p in client.iter_book_pages(max_books=1)]

    assert json.loads(route.calls.last.request.content)["pagination"]["size"] == 1


async def test_iter_book_pages_stops_on_empty_page(respx_mock):
    route = respx_mock.post(f"{BASE}/api/v1/books/query").mock(return_value=_page([], 10))
    client = BookOrbitClient(BASE, KEY)

    pages = [p async for p in client.iter_book_pages(max_books=50)]

    assert len(pages) == 1 and route.call_count == 1


async def test_iter_book_pages_validates_before_sending(respx_mock):
    route = respx_mock.post(f"{BASE}/api/v1/books/query")
    client = BookOrbitClient(BASE, KEY)

    with pytest.raises(BookOrbitError, match="unknown filter field"):
        [p async for p in client.iter_book_pages(max_books=5, filter=all_of(rule("x", "eq", 1)))]
    assert not route.called


# --- card summaries ---------------------------------------------------------


def test_summarize_card():
    card = {
        "id": 7,
        "libraryId": 1,
        "title": "Dune",
        "authors": ["Frank Herbert"],
        "seriesName": "Dune",
        "seriesIndex": "1",
        "readStatus": {"status": "read", "source": "manual", "startedAt": None},
        "rating": 4.5,
        "readingProgress": 100,
        "genres": ["SF"],
        "tags": ["classic"],
        "publishedYear": 1965,
        "files": [
            {"id": 2, "format": "azw3", "role": "content"},
            {"id": 1, "format": "epub", "role": "primary"},
            {"id": 3, "format": "epub", "role": "content"},
        ],
    }
    s = server._summarize_card(card)
    assert s.readStatus == "read"
    assert s.formats == ["epub", "azw3"]
    assert (s.series, s.seriesIndex, s.rating) == ("Dune", "1", 4.5)


def test_summarize_card_handles_sparse_card():
    s = server._summarize_card({"id": 1, "readStatus": None, "files": None})
    assert s.readStatus is None and s.formats == [] and s.authors == []
