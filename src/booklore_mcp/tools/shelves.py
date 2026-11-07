"""Shelf read/write tools: listing, membership, shelf lifecycle, and plan-driven
organisation."""

from __future__ import annotations

from collections import Counter
from typing import Any

from fastmcp import Context

from .. import runtime
from ..app import mcp
from ..client import BookLoreError
from ..helpers import _all_books, _book_kind, _summarize_book
from ..models import BookSummary


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List shelves"})
async def list_shelves() -> list[dict]:
    """List all shelves (id, name, icon, public flag). Use get_shelf_books to see
    the books on a given shelf."""
    return await runtime.client.get("/api/v1/shelves") or []


@mcp.tool(
    annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Get books on a shelf"}
)
async def get_shelf_books(shelf_id: int) -> list[BookSummary]:
    """List the books on a shelf, as trimmed summaries. Get shelf IDs from
    list_shelves."""
    books = await runtime.client.get(f"/api/v1/shelves/{shelf_id}/books") or []
    return [_summarize_book(b) for b in books]


@mcp.tool(annotations={"idempotentHint": True, "title": "Assign / unassign shelves"})
async def assign_shelves(
    book_ids: list[int],
    add_shelf_ids: list[int] | None = None,
    remove_shelf_ids: list[int] | None = None,
) -> list[dict]:
    """Add and/or remove books from shelves in one call. Get shelf IDs from
    list_shelves and book IDs from list_books. Returns the affected books."""
    return await runtime.client.post(
        "/api/v1/books/shelves",
        json={
            "bookIds": book_ids,
            "shelvesToAssign": add_shelf_ids or [],
            "shelvesToUnassign": remove_shelf_ids or [],
        },
    )


@mcp.tool(annotations={"title": "Create shelf"})
async def create_shelf(name: str, icon: str | None = None, public_shelf: bool = False) -> dict:
    """Create a new shelf. Returns the created shelf including its new ID."""
    body: dict[str, Any] = {"name": name, "publicShelf": public_shelf}
    if icon:
        body["icon"] = icon
    return await runtime.client.post("/api/v1/shelves", json=body)


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "title": "Delete shelf"})
async def delete_shelf(shelf_id: int) -> str:
    """Delete a shelf by ID. Does NOT delete the books on it. This cannot be
    undone — confirm the shelf ID with list_shelves first."""
    await runtime.client.delete(f"/api/v1/shelves/{shelf_id}")
    return f"Deleted shelf {shelf_id}."


@mcp.tool(annotations={"idempotentHint": True, "title": "Add books to shelves"})
async def add_to_shelves(book_ids: list[int], shelf_ids: list[int]) -> list[dict]:
    """Append books to shelves without disturbing their other shelves (idempotent).
    The additive counterpart to assign_shelves. Returns the affected books."""
    return await runtime.client.post(
        "/api/v1/books/shelves",
        json={"bookIds": book_ids, "shelvesToAssign": shelf_ids, "shelvesToUnassign": []},
    )


def _validate_plan(plan: list[dict]) -> list[dict]:
    """Normalize an organize_shelves plan, rejecting malformed or duplicate entries."""
    if not plan:
        raise BookLoreError("`plan` is empty — pass at least one {shelf, book_ids} entry.")
    entries: list[dict] = []
    seen: set[str] = set()
    for i, entry in enumerate(plan):
        name = entry.get("shelf") if isinstance(entry, dict) else None
        ids = entry.get("book_ids") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise BookLoreError(f"plan[{i}] needs a non-empty 'shelf' name.")
        if not isinstance(ids, list) or not all(isinstance(x, int) for x in ids):
            raise BookLoreError(f"plan[{i}] ('{name}') needs 'book_ids' as a list of integers.")
        if name.strip().lower() in seen:
            raise BookLoreError(f"Shelf '{name}' appears more than once in the plan; merge them.")
        seen.add(name.strip().lower())
        entries.append(
            {
                "shelf": name.strip(),
                "icon": entry.get("icon"),
                "public": bool(entry.get("public", False)),
                "book_ids": list(dict.fromkeys(ids)),  # de-duplicate, keep order
            }
        )
    return entries


@mcp.tool(annotations={"idempotentHint": True, "title": "Organize books into shelves"})
async def organize_shelves(
    plan: list[dict], dry_run: bool = True, ctx: Context | None = None
) -> dict:
    """Create shelves and file books/comics into them from a plan — preview first.

    Intended workflow: gather context about the library (e.g. export_library as CSV with
    fields id,title,authors,series,categories,kind, or search_books / list_genres), draft
    a shelf scheme for what the user asked for, call this with dry_run=True (the default)
    and SHOW THE USER the preview. Only call again with dry_run=False after the user has
    explicitly approved (or adjusted) the plan. The prompt `organize_library` walks
    through this.

    `plan` is [{"shelf": str, "book_ids": [int], "icon"?: str, "public"?: bool}]. Shelf
    names are matched case-insensitively to existing shelves; unmatched ones are created.
    The preview reports, per shelf, whether it will be created or reused, the books to
    add, those already on it, unknown ids, and counts split into books/comics/audiobooks
    — nothing is written. Applying is additive only: books are added to shelves, never
    removed from any shelf, so it is safe to re-run. Partial success is reported per shelf.
    Returns the preview {dry_run, shelves, totals, plan}, or after applying {dry_run,
    created, assigned, failed}.
    """
    entries = _validate_plan(plan)
    shelves = await runtime.client.get("/api/v1/shelves") or []
    by_name = {str(s["name"]).lower(): s for s in shelves if isinstance(s, dict) and s.get("name")}
    books = {b.get("id"): b for b in await _all_books()}

    preview: list[dict] = []
    for e in entries:
        existing = by_name.get(e["shelf"].lower())
        shelf_id = existing.get("id") if existing else None
        unknown = [i for i in e["book_ids"] if i not in books]
        known = [i for i in e["book_ids"] if i in books]
        on_shelf = [
            i
            for i in known
            if shelf_id is not None
            and any(
                isinstance(s, dict) and s.get("id") == shelf_id
                for s in (books[i].get("shelves") or [])
            )
        ]
        to_add = [i for i in known if i not in on_shelf]
        kinds = Counter(_book_kind(books[i]) for i in to_add)
        preview.append(
            {
                "shelf": existing.get("name") if existing else e["shelf"],
                "action": "existing" if existing else "create",
                "shelf_id": shelf_id,
                "to_add": to_add,
                "already_on_shelf": on_shelf,
                "unknown_ids": unknown,
                "counts": {
                    "books": kinds["book"],
                    "comics": kinds["comic"],
                    "audiobooks": kinds["audiobook"],
                },
            }
        )

    if dry_run:
        return {
            "dry_run": True,
            "shelves": preview,
            "totals": {
                "shelves_to_create": sum(1 for p in preview if p["action"] == "create"),
                "shelves_reused": sum(1 for p in preview if p["action"] == "existing"),
                "books_to_add": sum(len(p["to_add"]) for p in preview),
                "unknown_ids": sum(len(p["unknown_ids"]) for p in preview),
            },
            "plan": entries,
        }

    created: list[dict] = []
    assigned: list[dict] = []
    failed: list[dict] = []
    for done, (e, p) in enumerate(zip(entries, preview, strict=True), start=1):
        try:
            shelf_id = p["shelf_id"]
            if shelf_id is None:
                body: dict[str, Any] = {"name": e["shelf"], "publicShelf": e["public"]}
                if e["icon"]:
                    body["icon"] = e["icon"]
                new = await runtime.client.post("/api/v1/shelves", json=body) or {}
                shelf_id = new.get("id")
                if shelf_id is None:
                    raise BookLoreError("BookLore did not return an id for the new shelf.")
                created.append({"shelf": e["shelf"], "shelf_id": shelf_id})
            if p["to_add"]:
                await runtime.client.post(
                    "/api/v1/books/shelves",
                    json={
                        "bookIds": p["to_add"],
                        "shelvesToAssign": [shelf_id],
                        "shelvesToUnassign": [],
                    },
                )
            assigned.append({"shelf": p["shelf"], "shelf_id": shelf_id, "added": p["to_add"]})
        except Exception as exc:
            failed.append({"shelf": p["shelf"], "error": str(exc)})
        finally:
            if ctx is not None:
                await ctx.report_progress(progress=done, total=len(entries))
    return {"dry_run": False, "created": created, "assigned": assigned, "failed": failed}


@mcp.tool(annotations={"idempotentHint": True, "title": "Remove books from shelves"})
async def remove_from_shelves(book_ids: list[int], shelf_ids: list[int]) -> list[dict]:
    """Remove books from shelves without disturbing their other shelves (idempotent).
    Returns the affected books."""
    return await runtime.client.post(
        "/api/v1/books/shelves",
        json={"bookIds": book_ids, "shelvesToAssign": [], "shelvesToUnassign": shelf_ids},
    )
