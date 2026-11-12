"""Tests for the search/discovery helpers and the new search/bulk/additive tools."""

from __future__ import annotations

import httpx
from fastmcp import Client

import server

BASE = "http://bookorbit.test"


# --- pure helper unit tests (no I/O) ----------------------------------------


def test_normalize_goodreads_id_strips_slug_keeps_numeric():
    assert server._normalize_goodreads_id("23463279-designing-data-intensive") == "23463279"
    assert server._normalize_goodreads_id("171712768") == "171712768"
    assert server._normalize_goodreads_id("  42  ") == "42"
    assert server._normalize_goodreads_id(None) is None


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
