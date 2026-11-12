"""Bulk and library tools: bulk_update_metadata (grouped, native bulk edit),
refresh_library and book_dock_rescan."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

import server

BASE = "http://bookorbit.test"
EDIT = f"{BASE}/api/v1/books/bulk-edit-metadata"


async def _call(name: str, args: dict):
    async with Client(server.mcp) as c:
        return await c.call_tool(name, args)


def _edit_result(fields: dict[str, int] | None = None, updated: int = 1) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "updatedBooks": updated,
            "fields": {
                f: {"updated": updated, "skippedLocked": n} for f, n in (fields or {}).items()
            },
        },
    )


def _bodies(route) -> list[dict]:
    return [json.loads(c.request.content) for c in route.calls]


# --- planning -------------------------------------------------------------------------------


def test_plan_routes_bulk_fields_to_bulk_edit_and_the_rest_to_patch():
    payloads, body = server._bulk_edit_plan(
        {
            "tags": {"add": ["a"], "remove": ["b"]},
            "genres": {"add": ["SF"]},
            "metadata": {"publisher": "P", "authors": ["Ann"], "isbn13": "978-0441013593"},
            "clear_fields": ["language", "subtitle"],
        }
    )
    assert payloads == [
        {
            "tags": {"mode": "add", "values": ["a"]},
            "genres": {"mode": "add", "values": ["SF"]},
            "authors": {"mode": "replace", "values": ["Ann"]},
            "publisher": {"value": "P"},
            "language": {"value": None},
        },
        {"tags": {"mode": "remove", "values": ["b"]}},
    ]
    assert body == {"isbn13": "978-0441013593", "subtitle": None}


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({}, "at least one of"),
        ({"categories": {"add": ["x"]}}, "categories -> genres"),
        ({"tags": ["x"]}, '"tags" takes'),
        ({"tags": {"add": ["x"]}, "metadata": {"tags": ["y"]}}, "both replaced and merged"),
        ({"tags": {"add": [" "]}}, "changes nothing"),
    ],
)
def test_plan_rejects_bad_patches(patch, match):
    with pytest.raises(server.BookOrbitError, match=match):
        server._bulk_edit_plan(patch)


# --- bulk_update_metadata ---------------------------------------------------------------------


async def test_shared_patch_is_one_bulk_call_for_all_books(authed):
    edit = authed.post(EDIT).mock(return_value=_edit_result({"tags": 1}, updated=2))
    patch_route = authed.patch(url__regex=rf"{BASE}/api/v1/books/\d+/metadata")

    d = (
        await _call(
            "bulk_update_metadata",
            {"book_ids": [1, 2, 3], "patch": {"tags": {"add": ["to-read"]}}},
        )
    ).data

    assert _bodies(edit) == [
        {"bookIds": [1, 2, 3], "fields": {"tags": {"mode": "add", "values": ["to-read"]}}}
    ]
    assert not patch_route.called
    assert d == {"updated": [1, 2, 3], "failed": [], "skipped_locked": {"tags": 1}}


async def test_items_are_grouped_by_identical_patch(authed):
    edit = authed.post(EDIT).mock(return_value=_edit_result())
    patches = {
        b: authed.patch(f"{BASE}/api/v1/books/{b}/metadata").mock(
            return_value=httpx.Response(200, json={"id": b})
        )
        for b in (2, 4)
    }

    d = (
        await _call(
            "bulk_update_metadata",
            {
                "items": [
                    {"book_id": 1, "patch": {"genres": {"add": ["SF"]}}},
                    {"book_id": 2, "patch": {"metadata": {"isbn13": "1"}}},
                    {"book_id": 3, "patch": {"genres": {"add": ["SF"]}}},
                    {"book_id": 4, "patch": {"metadata": {"isbn13": "2"}}},
                ]
            },
        )
    ).data

    assert _bodies(edit) == [
        {"bookIds": [1, 3], "fields": {"genres": {"mode": "add", "values": ["SF"]}}}
    ]
    assert json.loads(patches[2].calls.last.request.content) == {"isbn13": "1"}
    assert json.loads(patches[4].calls.last.request.content) == {"isbn13": "2"}
    assert d["updated"] == [1, 2, 3, 4] and d["failed"] == []


async def test_partial_failures_keep_input_order(authed):
    authed.post(EDIT).mock(
        return_value=httpx.Response(403, json={"message": "Missing permission: x"})
    )
    authed.patch(f"{BASE}/api/v1/books/5/metadata").mock(return_value=httpx.Response(200, json={}))
    authed.patch(f"{BASE}/api/v1/books/6/metadata").mock(return_value=httpx.Response(404))

    d = (
        await _call(
            "bulk_update_metadata",
            {
                "items": [
                    {"book_id": 5, "patch": {"metadata": {"isbn13": "1"}}},
                    {"book_id": 1, "patch": {"tags": {"add": ["x"]}}},
                    {"book_id": "x", "patch": {"tags": {"add": ["x"]}}},
                    {"book_id": 6, "patch": {"metadata": {"isbn13": "1"}}},
                    {"book_id": 7, "patch": {"categories": {"add": ["y"]}}},
                ]
            },
        )
    ).data

    assert d["updated"] == [5]
    assert [f["book_id"] for f in d["failed"]] == [1, "x", 6, 7]
    errors = [f["error"] for f in d["failed"]]
    assert "Missing permission: x" in errors[0]
    assert "integer 'book_id'" in errors[1]
    assert "404" in errors[2]
    assert "categories -> genres" in errors[3]


async def test_large_groups_are_chunked(authed, monkeypatch):
    monkeypatch.setattr(server, "BULK_CHUNK_SIZE", 2)
    edit = authed.post(EDIT).mock(return_value=_edit_result())
    await _call("bulk_update_metadata", {"book_ids": [1, 2, 3], "patch": {"tags": {"add": ["x"]}}})
    assert [b["bookIds"] for b in _bodies(edit)] == [[1, 2], [3]]


async def test_book_ids_and_items_are_exclusive(authed):
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "bulk_update_metadata",
            {"book_ids": [1], "patch": {"tags": {"add": ["x"]}}, "items": [{"book_id": 1}]},
            raise_on_error=False,
        )
    assert result.is_error and "not both" in result.content[0].text


# --- library tools ------------------------------------------------------------------------------

SCAN = f"{BASE}/api/v1/scanner/libraries/1"


async def test_refresh_library_returns_immediately_by_default(authed):
    start = authed.post(f"{SCAN}/scan").mock(return_value=httpx.Response(202, json={"jobId": 11}))
    history = authed.get(f"{SCAN}/scan-history")

    d = (await _call("refresh_library", {"library_id": 1})).data

    assert start.called and not history.called
    assert d == {"library_id": 1, "job_id": 11, "status": "running"}


async def test_refresh_library_waits_for_the_scan(authed, monkeypatch):
    monkeypatch.setattr(server, "SCAN_POLL_INTERVAL", 0)
    authed.post(f"{SCAN}/scan").mock(return_value=httpx.Response(202, json={"jobId": 11}))
    running = {"id": 11, "status": "running", "addedCount": 0}
    done = {
        **running,
        "status": "completed",
        "addedCount": 2,
        "updatedCount": 1,
        "missingCount": 0,
        "errorMessage": None,
    }
    authed.get(f"{SCAN}/scan-history").mock(
        side_effect=[
            httpx.Response(200, json=[{"id": 10, "status": "completed"}]),
            httpx.Response(200, json=[running]),
            httpx.Response(200, json=[done, {"id": 10, "status": "completed"}]),
        ]
    )

    d = (await _call("refresh_library", {"library_id": 1, "wait_seconds": 30})).data

    assert (d["job_id"], d["status"], d["added"], d["updated"]) == (11, "completed", 2, 1)


async def test_refresh_library_reports_conflict(authed):
    authed.post(f"{SCAN}/scan").mock(
        return_value=httpx.Response(
            409, json={"message": "A scan is already running for library 1"}
        )
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool("refresh_library", {"library_id": 1}, raise_on_error=False)
    assert result.is_error and "already running" in result.content[0].text


async def test_book_dock_rescan_with_summary(authed):
    rescan = authed.post(f"{BASE}/api/v1/book-dock/rescan").mock(return_value=httpx.Response(204))
    authed.get(f"{BASE}/api/v1/book-dock/summary").mock(
        return_value=httpx.Response(200, json={"pending": 3})
    )
    d = (await _call("book_dock_rescan", {})).data
    assert rescan.called and d == {
        "status": "book dock rescan triggered",
        "summary": {"pending": 3},
    }


async def test_book_dock_rescan_without_read_access(authed):
    authed.post(f"{BASE}/api/v1/book-dock/rescan").mock(return_value=httpx.Response(204))
    authed.get(f"{BASE}/api/v1/book-dock/summary").mock(
        return_value=httpx.Response(403, json={"message": "Missing permission: book_dock_access"})
    )
    d = (await _call("book_dock_rescan", {})).data
    assert d == {"status": "book dock rescan triggered"}


async def test_bulk_set_read_status_groups_by_status(authed):
    route = authed.post(f"{BASE}/api/v1/books/bulk-set-status").mock(
        return_value=httpx.Response(204)
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool(
            "bulk_set_read_status",
            {
                "items": [
                    {"book_id": 1, "status": "read"},
                    {"book_id": 2, "status": "read"},
                    {"book_id": 3, "status": "reading"},
                    {"book_id": 4, "status": "READ"},
                ]
            },
        )

    d = result.data
    assert sorted(d["updated"]) == [1, 2, 3]
    assert d["by_status"] == {"read": 2, "reading": 1}
    assert d["failed"][0]["book_id"] == 4
    assert route.call_count == 2  # one call per distinct valid status, not per book
