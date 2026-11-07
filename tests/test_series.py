"""Tests for find_series_gaps: missing volumes/issues in book and comic series."""

from __future__ import annotations

import httpx
import pytest
from fastmcp import Client

from booklore_mcp import runtime
from booklore_mcp.app import mcp
from booklore_mcp.client import BookLoreClient

BASE = "http://booklore.test"


@pytest.fixture
def authed(respx_mock):
    respx_mock.post(f"{BASE}/api/v1/auth/login").mock(
        return_value=httpx.Response(200, json={"accessToken": "a", "refreshToken": "r"})
    )
    runtime.client = BookLoreClient(BASE, "tester", "secret")
    return respx_mock


def _book(id_, series, number=None, total=None, **extra):
    meta = {"seriesName": series}
    if number is not None:
        meta["seriesNumber"] = number
    if total is not None:
        meta["seriesTotal"] = total
    meta.update(extra.pop("meta", {}))
    return {"id": id_, "title": f"{series} {number}", "metadata": meta, **extra}


async def _gaps(authed, books, **args):
    route = authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))
    async with Client(mcp) as c:
        result = await c.call_tool("find_series_gaps", args)
    return result.data, route


async def test_known_total_reports_inner_and_trailing_gaps(authed):
    books = [_book(1, "Expanse", 1, 9), _book(2, "Expanse", 3, 9), _book(3, "Expanse", 4)]
    data, route = await _gaps(authed, books)

    s = data["series"][0]
    assert (s["series"], s["series_total"], s["total_known"]) == ("Expanse", 9, True)
    assert s["owned"] == [1, 3, 4]
    assert s["missing"] == [2, 5, 6, 7, 8, 9]
    assert s["complete"] is False and data["incomplete_count"] == 1
    # seriesTotal is stripped from the plain list response -> full records requested.
    assert route.calls.last.request.url.params["withDescription"] == "true"


async def test_unknown_total_only_finds_gaps_inside_owned_run(authed):
    books = [_book(1, "Dune", 1), _book(2, "Dune", 2.5), _book(3, "Dune", 4)]
    data, _ = await _gaps(authed, books)

    s = data["series"][0]
    assert s["total_known"] is False
    assert s["owned"] == [1, 2.5, 4]
    assert s["missing"] == [2, 3]  # half-number 2.5 is an extra, not a fill for 2 or 3


async def test_comic_issue_number_fallback_and_unnumbered(authed):
    books = [
        _book(
            1, "Saga", primaryFile={"bookType": "CBX"}, meta={"comicMetadata": {"issueNumber": "1"}}
        ),
        _book(
            2, "Saga", primaryFile={"bookType": "CBX"}, meta={"comicMetadata": {"issueNumber": "3"}}
        ),
        _book(
            3,
            "Saga",
            primaryFile={"bookType": "CBX"},
            meta={"comicMetadata": {"issueNumber": "Annual"}},
        ),
    ]
    data, _ = await _gaps(authed, books, kind="comic")

    s = data["series"][0]
    assert s["kind"] == "comic"
    assert s["owned"] == [1, 3] and s["missing"] == [2]
    assert s["unnumbered"] == [3]


async def test_complete_series_hidden_unless_requested_and_series_filter(authed):
    books = [
        _book(1, "Trilogy", 1, 3),
        _book(2, "trilogy", 2),  # same series, different casing
        _book(3, "Trilogy", 3),
        _book(4, "Other", 2),
    ]
    authed.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=books))
    # One session: the server lifespan closes the shared client when a session ends.
    async with Client(mcp) as c:
        default = (await c.call_tool("find_series_gaps", {})).data
        only = (
            await c.call_tool("find_series_gaps", {"include_complete": True, "series": "TRILOGY"})
        ).data

    assert [s["series"] for s in default["series"]] == ["Other"]  # complete Trilogy omitted
    assert len(only["series"]) == 1
    assert only["series"][0]["complete"] is True and only["series"][0]["missing"] == []


async def test_unknown_series_name_returns_suggestions(authed):
    books = [_book(1, "The Expanse", 1), _book(2, "The Expanse", 2), _book(3, "Discworld", 1)]
    data, _ = await _gaps(authed, books, series="Expanse", include_complete=True)

    assert data["series_count"] == 0
    assert data["suggestions"][0]["name"] == "The Expanse"
    assert data["suggestions"][0]["book_count"] == 2


async def test_known_series_name_has_no_suggestions(authed):
    data, _ = await _gaps(authed, [_book(1, "Discworld", 1)], series="discworld")
    assert "suggestions" not in data
