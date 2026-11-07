"""Library & server tools: listing libraries, triggering rescans, liveness/auth probe."""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from .. import runtime
from ..app import mcp
from ..client import BookLoreError
from ..config import USERNAME


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List libraries"})
async def list_libraries() -> list[dict]:
    """List BookLore libraries and their on-disk paths.

    Files are ingested into a library *path* on the BookLore host (e.g. by dropping
    them into a watched folder); this surfaces each library's id, name, whether it's
    watched, its paths (id + filesystem path), and allowed formats — the context you
    need to know where a manually-added book will land and which library to rescan.
    Returns [{id, name, watch, paths:[{id, path}], allowed_formats}].
    """
    libs = await runtime.client.get("/api/v1/libraries") or []
    out: list[dict] = []
    for lib in libs:
        if not isinstance(lib, dict):
            continue
        out.append(
            {
                "id": lib.get("id"),
                "name": lib.get("name"),
                "watch": lib.get("watch"),
                "paths": [
                    {"id": p.get("id"), "path": p.get("path")}
                    for p in (lib.get("paths") or [])
                    if isinstance(p, dict)
                ],
                "allowed_formats": lib.get("allowedFormats"),
            }
        )
    return out


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True, "title": "Refresh library"})
async def refresh_library(library_id: int) -> dict:
    """Trigger a rescan of a library's paths so BookLore ingests files added on disk.

    Use after manually placing a book (e.g. a safaribooks EPUB) into one of the
    library's paths — BookLore re-walks the paths, imports new files and reads their
    embedded metadata. Scanning runs server-side and may take a moment to reflect in
    the book list. Use `list_libraries` to find the id. Returns {library_id, status}.
    """
    await runtime.client.put(f"/api/v1/libraries/{library_id}/refresh")
    return {"library_id": library_id, "status": "rescan triggered"}


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True, "title": "Rescan bookdrop"})
async def bookdrop_rescan() -> dict:
    """Trigger a rescan of BookLore's bookdrop folder for files added on disk.

    BookLore watches a dedicated "bookdrop" folder; files dropped there are staged for
    review before import. Use this to pick up files added while the watcher was down or
    that it missed. Staged files still need finalizing in the BookLore UI before they
    enter a library. Returns {status}.
    """
    await runtime.client.post("/api/v1/bookdrop/rescan")
    return {"status": "bookdrop rescan triggered"}


@mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": True, "title": "Ping / whoami"})
async def ping() -> dict:
    """Liveness + auth probe. `ok` false means the server is unreachable; ok=true with
    authenticated=false means reachable but the login was rejected or credentials are
    unset — so the client can tell "down" from "logged out" before a long batch.

    Reachability + version come from the unauthenticated /healthcheck; user,
    book_count and library_count are added once authenticated.
    """
    result: dict[str, Any] = {"ok": False, "authenticated": False}

    # Unauthenticated liveness probe — distinguishes "down" from "logged out".
    try:
        health = (await runtime.client._http.get("/api/v1/healthcheck")).json()
    except Exception as exc:  # transport-level failure
        result["error"] = str(exc)
        return result
    result["ok"] = True
    payload = health.get("data") if isinstance(health, dict) else None
    version = (payload or health or {}).get("version")
    if version:
        result["server_version"] = version

    # Auth probe.
    try:
        books = await runtime.client.get("/api/v1/books", params={"withDescription": "false"})
    except BookLoreError as exc:
        result["error"] = str(exc)
        return result
    result.update(authenticated=True, user=USERNAME, book_count=len(books or []))
    with suppress(BookLoreError):
        result["library_count"] = len(await runtime.client.get("/api/v1/libraries") or [])
    return result
