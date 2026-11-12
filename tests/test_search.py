"""Tests for the search/discovery helpers and the new search/bulk/additive tools."""

from __future__ import annotations

import json

import httpx
from fastmcp import Client

import server

BASE = "http://bookorbit.test"


# --- pure helper unit tests (no I/O) ----------------------------------------


def test_merge_list_add_remove_case_insensitive_preserves_order():
    merged = server._merge_list(
        ["Python", "narrative"], add=["python", "trading"], remove=["NARRATIVE"]
    )
    assert merged == ["Python", "trading"]


def test_normalize_goodreads_id_strips_slug_keeps_numeric():
    assert server._normalize_goodreads_id("23463279-designing-data-intensive") == "23463279"
    assert server._normalize_goodreads_id("171712768") == "171712768"
    assert server._normalize_goodreads_id("  42  ") == "42"
    assert server._normalize_goodreads_id(None) is None


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


async def test_ping_reports_identity_version_and_counts(authed):
    authed.get(f"{BASE}/api/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    me = authed.get(f"{BASE}/api/v1/auth/me").mock(
        return_value=httpx.Response(
            200,
            json={"username": "ann", "authenticationMethod": "api_key", "permissions": ["x"]},
        )
    )
    authed.get(f"{BASE}/api/v1/app-info").mock(
        return_value=httpx.Response(200, json={"version": "v3.8.0", "updateAvailable": False})
    )
    authed.get(f"{BASE}/api/v1/libraries").mock(return_value=httpx.Response(200, json=[{"id": 1}]))
    authed.get(f"{BASE}/api/v1/statistics/summary").mock(
        return_value=httpx.Response(200, json={"totalBooks": 433})
    )

    async with Client(server.mcp) as c:
        result = await c.call_tool("ping", {})

    d = result.data
    assert d["ok"] is True and d["authenticated"] is True
    assert d["user"] == "ann" and d["authentication_method"] == "api_key"
    assert d["permissions"] == ["x"]
    assert d["server_version"] == "v3.8.0" and "latest_version" not in d
    assert d["library_count"] == 1 and d["book_count"] == 433
    assert me.calls.last.request.headers["X-Api-Key"].startswith("bok_")


async def test_ping_up_but_key_rejected(authed):
    authed.get(f"{BASE}/api/v1/health").mock(
        return_value=httpx.Response(200, json={"status": "ok"})
    )
    authed.get(f"{BASE}/api/v1/auth/me").mock(
        return_value=httpx.Response(401, json={"statusCode": 401, "message": "Unauthorized"})
    )

    async with Client(server.mcp) as c:
        result = await c.call_tool("ping", {})

    d = result.data
    assert d["ok"] is True and d["authenticated"] is False
    assert "API key was rejected" in d["error"]


async def test_ping_reports_unreachable(authed):
    authed.get(f"{BASE}/api/v1/health").mock(side_effect=httpx.ConnectError("refused"))

    async with Client(server.mcp) as c:
        result = await c.call_tool("ping", {})

    assert result.data["ok"] is False and "refused" in result.data["error"]


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
