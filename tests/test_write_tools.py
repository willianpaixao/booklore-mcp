"""Single-book write tools (and bulk_update_metadata, which shares their patch
logic), end to end through FastMCP's in-memory client against respx mocks."""

from __future__ import annotations

import json

import httpx
import pytest
from fastmcp import Client

import server

BASE = "http://bookorbit.test"


async def _call(name: str, args: dict):
    async with Client(server.mcp) as c:
        return await c.call_tool(name, args)


async def _error(name: str, args: dict) -> str:
    async with Client(server.mcp) as c:
        result = await c.call_tool(name, args, raise_on_error=False)
    assert result.is_error
    return result.content[0].text


def _body(route) -> dict:
    return json.loads(route.calls.last.request.content)


def _edit_result(field: str, updated: int = 1, skipped: int = 0) -> httpx.Response:
    return httpx.Response(
        201,
        json={
            "updatedBooks": updated,
            "fields": {field: {"updated": updated, "skippedLocked": skipped}},
        },
    )


# --- update_book_metadata -----------------------------------------------------------


async def test_update_metadata_patches_only_given_fields_and_clears(authed):
    route = authed.patch(f"{BASE}/api/v1/books/5/metadata").mock(
        return_value=httpx.Response(200, json={"id": 5, "title": "X"})
    )

    result = await _call(
        "update_book_metadata",
        {
            "book_id": 5,
            "metadata": {"title": "X", "goodreadsId": "23463279-designing-data"},
            "clear_fields": ["subtitle", "tags"],
        },
    )

    assert _body(route) == {
        "title": "X",
        "goodreadsId": "23463279",
        "subtitle": None,
        "tags": [],
    }
    assert result.data == {"id": 5, "title": "X"}


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"metadata": {"categories": ["x"]}}, "categories -> genres"),
        ({"metadata": {"asin": "B0"}}, "asin -> amazonId"),
        ({"clear_fields": ["audioMetadata"]}, "Unknown clearable field"),
        ({"metadata": {"title": "x"}, "clear_fields": ["title"]}, "both set and cleared"),
        ({}, "at least one field"),
    ],
)
async def test_update_metadata_rejects_bad_input_before_sending(authed, args, match):
    route = authed.patch(f"{BASE}/api/v1/books/5/metadata")
    assert match in await _error("update_book_metadata", {"book_id": 5, **args})
    assert not route.called


# --- status, rating ---------------------------------------------------------------------


async def test_set_read_status_bulk_with_dates(authed):
    route = authed.post(f"{BASE}/api/v1/books/bulk-set-status").mock(
        return_value=httpx.Response(204)
    )
    result = await _call(
        "set_read_status",
        {"book_ids": [1, 2], "status": "read", "finished_at": "2026-11-01"},
    )
    assert _body(route) == {"bookIds": [1, 2], "status": "read", "finishedAt": "2026-11-01"}
    assert result.data == {"book_ids": [1, 2], "status": "read"}


async def test_set_read_status_rejects_old_values_and_bad_dates(authed):
    assert (
        "validation error"
        in (await _error("set_read_status", {"book_ids": [1], "status": "READ"})).lower()
    )
    assert "startedAt must be" in await _error(
        "set_read_status", {"book_ids": [1], "status": "reading", "started_at": "yesterday"}
    )


@pytest.mark.parametrize("rating", [4, None])
async def test_set_personal_rating(authed, rating):
    route = authed.post(f"{BASE}/api/v1/books/bulk-set-rating").mock(
        return_value=httpx.Response(204)
    )
    await _call("set_personal_rating", {"book_ids": [3], "rating": rating})
    assert _body(route) == {"bookIds": [3], "rating": rating}


async def test_set_personal_rating_range(authed):
    assert "1 to 5" in await _error("set_personal_rating", {"book_ids": [3], "rating": 8})


# --- tags and genres ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tool", "field", "mode", "arg"),
    [
        ("add_tags", "tags", "add", "tags"),
        ("remove_tags", "tags", "remove", "tags"),
        ("add_genres", "genres", "add", "genres"),
        ("remove_genres", "genres", "remove", "genres"),
    ],
)
async def test_tag_and_genre_tools_use_bulk_edit(authed, tool, field, mode, arg):
    route = authed.post(f"{BASE}/api/v1/books/bulk-edit-metadata").mock(
        return_value=_edit_result(field, updated=1, skipped=1)
    )
    result = await _call(tool, {"book_ids": [1, 2], arg: ["Sci-Fi", " "]})
    assert _body(route) == {
        "bookIds": [1, 2],
        "fields": {field: {"mode": mode, "values": ["Sci-Fi"]}},
    }
    assert result.data == {"book_ids": [1, 2], "updated_books": 1, "skipped_locked": 1}


async def test_add_tags_needs_a_name(authed):
    assert "at least one tag" in await _error("add_tags", {"book_ids": [1], "tags": [""]})


# --- locks ------------------------------------------------------------------------------


async def test_set_field_locks_merges_with_existing_locks(authed):
    authed.get(f"{BASE}/api/v1/books/5").mock(
        return_value=httpx.Response(200, json={"id": 5, "lockedFields": ["tags", "cover"]})
    )
    route = authed.patch(f"{BASE}/api/v1/books/5/metadata-locks").mock(
        return_value=httpx.Response(200, json={})
    )
    result = await _call(
        "set_field_locks", {"book_ids": [5], "lock": ["title", "authors"], "unlock": ["cover"]}
    )
    assert _body(route) == {"lockedFields": ["title", "authors", "tags"]}
    assert result.structured_content["result"] == [
        {"book_id": 5, "locked_fields": ["title", "authors", "tags"]}
    ]


@pytest.mark.parametrize(("arg", "locked"), [("lock", True), ("unlock", False)])
async def test_set_field_locks_all_uses_bulk_route(authed, arg, locked):
    route = authed.post(f"{BASE}/api/v1/books/bulk-set-metadata-lock").mock(
        return_value=httpx.Response(204)
    )
    await _call("set_field_locks", {"book_ids": [1, 2], arg: ["all"]})
    assert _body(route) == {"bookIds": [1, 2], "locked": locked}


@pytest.mark.parametrize(
    ("args", "match"),
    [
        ({"lock": ["categories"]}, "categories -> genres"),
        ({"lock": ["title"], "unlock": ["title"]}, "both locked and unlocked"),
        ({"lock": ["all", "title"]}, '"all" must be the only entry'),
        ({}, "at least one field"),
    ],
)
async def test_set_field_locks_validates(authed, args, match):
    assert match in await _error("set_field_locks", {"book_ids": [1], **args})


# --- collections ---------------------------------------------------------------------------


async def test_create_collection_defaults(authed):
    route = authed.post(f"{BASE}/api/v1/collections").mock(
        return_value=httpx.Response(201, json={"id": 9, "name": "To read"})
    )
    result = await _call("create_collection", {"name": "To read"})
    assert _body(route) == {"name": "To read", "icon": "Library", "isPublic": False}
    assert result.data["id"] == 9


async def test_delete_collection(authed):
    authed.delete(f"{BASE}/api/v1/collections/3").mock(return_value=httpx.Response(204))
    assert "Deleted collection 3" in (await _call("delete_collection", {"collection_id": 3})).data


async def test_add_and_remove_collection_membership(authed):
    add = [
        authed.post(f"{BASE}/api/v1/collections/{cid}/books").mock(return_value=httpx.Response(204))
        for cid in (1, 2)
    ]
    remove = authed.delete(f"{BASE}/api/v1/collections/1/books").mock(
        return_value=httpx.Response(204)
    )

    async with Client(server.mcp) as c:  # one session: the lifespan closes the client
        await c.call_tool("add_to_collections", {"book_ids": [7, 8], "collection_ids": [1, 2]})
        await c.call_tool("remove_from_collections", {"book_ids": [7], "collection_ids": [1]})

    assert all(_body(r) == {"bookIds": [7, 8]} for r in add)
    assert _body(remove) == {"bookIds": [7]}


# --- reading progress -------------------------------------------------------------------------


async def test_set_reading_progress(authed):
    route = authed.post(f"{BASE}/api/v1/books/files/456/progress").mock(
        return_value=httpx.Response(201)
    )
    await _call(
        "set_reading_progress", {"book_file_id": 456, "percentage": 42.5, "page_number": 10}
    )
    assert _body(route) == {"percentage": 42.5, "pageNumber": 10}


async def test_set_reading_progress_range(authed):
    assert "between 0 and 100" in await _error(
        "set_reading_progress", {"book_file_id": 1, "percentage": 120}
    )


async def test_reset_progress_clears_each_file(authed):
    authed.get(f"{BASE}/api/v1/books/5/progress").mock(
        return_value=httpx.Response(200, json=[{"fileId": 10}, {"fileId": 11}])
    )
    authed.get(f"{BASE}/api/v1/books/6/progress").mock(return_value=httpx.Response(404))
    deletes = [
        authed.delete(f"{BASE}/api/v1/books/files/{fid}/progress").mock(
            return_value=httpx.Response(204)
        )
        for fid in (10, 11)
    ]

    d = (await _call("reset_progress", {"book_ids": [5, 6]})).data

    assert all(r.called for r in deletes)
    assert d["reset"] == [5] and d["failed"][0]["book_id"] == 6


async def test_reset_progress_everything(authed):
    route = authed.post(f"{BASE}/api/v1/books/5/reset-reading-state").mock(
        return_value=httpx.Response(201, json={})
    )
    d = (await _call("reset_progress", {"book_ids": [5], "scope": "everything"})).data
    assert route.called and d == {"scope": "everything", "reset": [5], "failed": []}
