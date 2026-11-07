"""Metadata tools: direct edits, additive tag/category merges, bulk patches, field
locks, external-provider lookups, and identifier cleanup."""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from contextlib import suppress
from typing import Any, Literal

import httpx
from fastmcp import Context

from .. import runtime
from ..app import mcp
from ..client import BookLoreError
from ..config import BULK_CONCURRENCY
from ..helpers import (
    _all_books,
    _apply_patch,
    _author_names,
    _meta,
    _normalize_goodreads_id,
    _normalize_metadata,
    _put_metadata,
    _validate_clear_fields,
)

# MetadataProvider enum name -> key under settings.metadataProviderSettings. Mostly a
# lowercased first letter, but GoodReads/Lubimyczytac don't follow that, so map explicitly.
_PROVIDER_SETTINGS_KEY = {
    "Amazon": "amazon",
    "Google": "google",
    "GoodReads": "goodReads",
    "Hardcover": "hardcover",
    "Comicvine": "comicvine",
    "Douban": "douban",
    "Lubimyczytac": "lubimyczytac",
    "Ranobedb": "ranobedb",
    "Audible": "audible",
}

# Provider enum name -> its provider-specific field toggles under
# settings.metadataProviderSpecificFields. These gate whether a field is *persisted* on
# auto-fetch/refresh; a disabled toggle leaves that field empty library-wide even when the
# provider runs (e.g. goodreadsRating off => no book ever gets a Goodreads rating).
_PROVIDER_SPECIFIC_FIELDS = {
    "Amazon": ["asin", "amazonRating", "amazonReviewCount"],
    "Google": ["googleId"],
    "GoodReads": ["goodreadsId", "goodreadsRating", "goodreadsReviewCount"],
    "Hardcover": ["hardcoverId", "hardcoverBookId", "hardcoverRating", "hardcoverReviewCount"],
    "Comicvine": ["comicvineId"],
    "Lubimyczytac": ["lubimyczytacId", "lubimyczytacRating"],
    "Ranobedb": ["ranobedbId", "ranobedbRating"],
    "Audible": ["audibleId", "audibleRating", "audibleReviewCount"],
    "Douban": [],
}


async def _app_settings() -> dict | None:
    """Best-effort read of the full app settings (provider enablement, Amazon cookie,
    provider-specific field toggles). Returns None when the endpoint is unavailable
    (e.g. permission denied), so callers degrade to result-only status rather than fail."""
    with suppress(BookLoreError):
        settings = await runtime.client.get("/api/v1/settings")
        if isinstance(settings, dict):
            return settings
    return None


def _provider_status(provider: str, count: int, settings: dict | None, queried: bool) -> dict:
    """Classify one requested provider's outcome as ok / empty / disabled, and note any of
    its provider-specific fields (ratings/ids) that are toggled off — those stay empty
    library-wide even when the provider returns candidates."""
    prov_cfg = (settings or {}).get("metadataProviderSettings") or {}
    field_cfg = (settings or {}).get("metadataProviderSpecificFields") or {}
    cfg = prov_cfg.get(_PROVIDER_SETTINGS_KEY.get(provider, ""), {}) or {}

    # Provider-specific fields explicitly disabled (only meaningful if we could read settings).
    disabled_fields = (
        [f for f in _PROVIDER_SPECIFIC_FIELDS.get(provider, []) if field_cfg.get(f) is False]
        if settings is not None
        else []
    )

    if settings is not None and _PROVIDER_SETTINGS_KEY.get(provider) and not cfg.get("enabled"):
        result = {
            "provider": provider,
            "status": "disabled",
            "count": 0,
            "reason": "provider is disabled in BookLore Settings > Metadata 1.",
        }
    elif count:
        result = {"provider": provider, "status": "ok", "count": count}
    else:
        # Enabled (or unknown) but produced nothing. A real query that still returns zero is
        # far more likely a blocked/broken scraper than a true no-match.
        if queried:
            reason = (
                "enabled but returned 0 candidates for a real query — likely blocked, "
                "rate-limited, mis-regioned, or the provider's scraper is broken "
                "server-side (the backend logs the error but the API does not expose it)."
            )
        else:
            reason = (
                "no candidates — no usable search terms (pass isbn/title or set them on the book)."
            )
        if provider == "Amazon" and not cfg.get("cookie"):
            reason += " Amazon often needs a session cookie/region (Settings > Metadata 1)."
        result = {"provider": provider, "status": "empty", "count": 0, "reason": reason}

    if disabled_fields:
        result["disabled_fields"] = disabled_fields
        result["fields_note"] = (
            f"these fields are toggled OFF in Settings > Metadata 2, so auto-fetch won't "
            f"persist them library-wide: {', '.join(disabled_fields)}."
        )
    return result


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
        "title": "Look up metadata by ISBN",
    }
)
async def isbn_lookup(isbn: str) -> dict:
    """Fetch book metadata for an ISBN from BookLore's external providers
    (Google Books, OpenLibrary, etc.). Read-only lookup — does not modify any
    book. Use update_book_metadata to apply the result to a book."""
    return await runtime.client.post("/api/v1/metadata/isbn-lookup", json={"isbn": isbn})


@mcp.tool(annotations={"title": "Update book metadata"})
async def update_book_metadata(
    book_id: int,
    metadata: dict | None = None,
    replace_mode: Literal["REPLACE_WHEN_PROVIDED", "REPLACE_MISSING", "REPLACE_ALL"] = (
        "REPLACE_WHEN_PROVIDED"
    ),
    merge_categories: bool = False,
    clear_fields: list[str] | None = None,
) -> dict:
    """Update a book's metadata. `metadata` is a BookMetadata object (e.g.
    {"title": ..., "authors": [...], "description": ..., "isbn13": ...}).

    Replace modes (default REPLACE_WHEN_PROVIDED is the safe one):
    - REPLACE_WHEN_PROVIDED — write only the fields you include; everything else
      is left exactly as-is. Use this for fill-in/enrichment.
    - REPLACE_MISSING — write a field only if it's currently empty (never
      overwrites existing values; won't touch a non-empty author list).
    - REPLACE_ALL — replace the WHOLE record with what you send: any field you
      omit is wiped to null. Destructive — only use when you pass the full record.

    To CLEAR a field, list it in `clear_fields` (e.g. ["amazonRating"]). BookLore
    nulls those fields regardless of replace_mode — under REPLACE_WHEN_PROVIDED a
    `null` in `metadata` is ignored, so clear_fields is the way to empty one field
    without REPLACE_ALL wiping the rest. `authors` is a list of name strings.
    `goodreadsId` is normalized to its bare numeric id. Returns the updated record.
    """
    _validate_clear_fields(clear_fields)
    metadata = _normalize_metadata(metadata or {})
    # `clearFlags` must be present: the server's @Builder.Default isn't applied
    # on JSON deserialization, so omitting it leaves it null and the API 500s.
    clear_flags = {f: True for f in (clear_fields or [])}
    return await runtime.client.put(
        f"/api/v1/books/{book_id}/metadata",
        params={"mergeCategories": str(merge_categories).lower(), "replaceMode": replace_mode},
        json={"metadata": metadata, "clearFlags": clear_flags},
    )


@mcp.tool(annotations={"title": "Bulk update metadata"})
async def bulk_update_metadata(
    book_ids: list[int] | None = None,
    patch: dict | None = None,
    items: list[dict] | None = None,
    ctx: Context | None = None,
) -> dict:
    """Apply per-book metadata patches to many books in one call.

    Either pass `book_ids` + a single `patch` (applied to all), or `items`
    (`[{"book_id": int, "patch": {...}}]`) for per-book patches — not both.
    A `patch` may combine:
      - "tags"/"categories": {"add": [...], "remove": [...]} — additive merges.
      - "metadata": {field: value, ...} — arbitrary BookMetadata fields (isbn13,
        language, publisher, seriesName, …) written with REPLACE_WHEN_PROVIDED.
      - "clear_fields": [field, ...] — fields to null out.
    This covers bulk fills beyond tags/categories (e.g. ISBNs). For read status use
    bulk_set_read_status. Partial success is reported per book; one bad id never
    fails the batch. Returns {updated:[ids], failed:[{book_id,error}], books:[records]}.
    """
    if items and (book_ids or patch):
        raise BookLoreError("Pass either book_ids+patch OR items, not both.")
    if not items and not (book_ids and patch):
        raise BookLoreError("Provide book_ids+patch, or items.")

    work = items if items else [{"book_id": bid, "patch": patch} for bid in (book_ids or [])]
    semaphore = asyncio.Semaphore(BULK_CONCURRENCY)
    total = len(work)
    done = 0

    async def _one(entry: dict) -> tuple[Any, Any, dict | None]:
        nonlocal done
        bid = entry.get("book_id")
        async with semaphore:
            try:
                if not isinstance(bid, int):
                    raise BookLoreError("each item needs an integer 'book_id'")
                return bid, await _apply_patch(bid, entry.get("patch") or {}), None
            except Exception as exc:
                return bid, None, {"book_id": bid, "error": str(exc)}
            finally:
                # Report as each book finishes (success or failure); tasks run
                # concurrently, so progress need not arrive in input order.
                done += 1
                if ctx is not None:
                    await ctx.report_progress(progress=done, total=total)

    updated: list[int] = []
    failed: list[dict] = []
    books: list[dict] = []
    # gather preserves input order, so results stay aligned with `work`.
    for bid, book, error in await asyncio.gather(*(_one(e) for e in work)):
        if error is not None:
            failed.append(error)
        else:
            updated.append(bid)
            books.append(book)
    return {"updated": updated, "failed": failed, "books": books}


@mcp.tool(annotations={"idempotentHint": True, "title": "Add tags"})
async def add_tags(book_id: int, tags: list[str]) -> dict:
    """Add tags to a book (merge; idempotent; existing tags untouched). Returns the
    post-write record. Safer than update_book_metadata for the common case."""
    return await _apply_patch(book_id, {"tags": {"add": tags}})


@mcp.tool(annotations={"idempotentHint": True, "title": "Remove tags"})
async def remove_tags(book_id: int, tags: list[str]) -> dict:
    """Remove tags from a book (idempotent; no error if a tag is absent). Returns the
    post-write record."""
    return await _apply_patch(book_id, {"tags": {"remove": tags}})


@mcp.tool(annotations={"idempotentHint": True, "title": "Add categories"})
async def add_categories(book_id: int, categories: list[str]) -> dict:
    """Add categories to a book (merge; idempotent). Returns the post-write record."""
    return await _apply_patch(book_id, {"categories": {"add": categories}})


@mcp.tool(annotations={"idempotentHint": True, "title": "Remove categories"})
async def remove_categories(book_id: int, categories: list[str]) -> dict:
    """Remove categories from a book (idempotent). Returns the post-write record."""
    return await _apply_patch(book_id, {"categories": {"remove": categories}})


@mcp.tool(annotations={"idempotentHint": True, "title": "Set metadata field locks"})
async def set_field_locks(
    book_ids: list[int],
    lock: list[str] | None = None,
    unlock: list[str] | None = None,
) -> list[dict]:
    """Lock or unlock metadata fields so curated values survive a metadata refresh.

    Field names are bare, e.g. "title", "authors", "tags", "categories",
    "description", "cover", "seriesName". Pass them in `lock` and/or `unlock`
    (a "Locked" suffix is added automatically). Returns updated metadata per book.
    """
    field_actions: dict[str, str] = {}
    for field in lock or []:
        field_actions[field if field.endswith("Locked") else f"{field}Locked"] = "LOCK"
    for field in unlock or []:
        field_actions[field if field.endswith("Locked") else f"{field}Locked"] = "UNLOCK"
    if not field_actions:
        raise BookLoreError("Provide at least one field in `lock` or `unlock`.")
    return await runtime.client.put(
        "/api/v1/books/metadata/toggle-field-locks",
        json={"bookIds": book_ids, "fieldActions": field_actions},
    )


@mcp.tool(
    annotations={"readOnlyHint": True, "openWorldHint": True, "title": "Fetch metadata candidates"}
)
async def fetch_metadata_candidates(
    book_id: int,
    providers: list[str] | None = None,
    isbn: str | None = None,
    title: str | None = None,
    author: str | None = None,
) -> dict:
    """Fetch candidate metadata for a book from external providers — read-only, does
    NOT modify the book.

    `providers` is any of: Amazon, GoodReads, Google, Hardcover, Comicvine, Douban,
    Lubimyczytac, Ranobedb, Audible (defaults to Google, GoodReads, Amazon). When you
    pass none of `isbn`/`title`/`author`, the book's stored ISBN/title/author are used
    as the query automatically — so book_id alone works.

    Each candidate is a full BookMetadata object carrying that provider's own fields:
    Amazon supplies asin/amazonRating, GoodReads supplies goodreadsId/goodreadsRating,
    Google supplies googleId — so to fill a given field you must include the provider
    that owns it. Review the candidates, then apply one with update_book_metadata
    (lock curated fields first with set_field_locks).

    `provider_status` reports each requested provider as ok (n candidates), empty (the
    provider ran but returned nothing — blocked, rate-limited, mis-regioned, or no
    match), or disabled (turned off in BookLore Settings > Metadata 1). This makes a dead
    provider distinguishable from a genuine no-match, which the raw stream cannot. An entry
    may also include `disabled_fields` — provider-specific fields (e.g. goodreadsRating)
    toggled off in Settings > Metadata 2, which auto-fetch won't persist library-wide even
    when the provider works.
    """
    provs = providers or ["Google", "GoodReads", "Amazon"]

    # P1: default the search terms from the book's own stored metadata when the caller
    # passes none — the backend otherwise falls back only to the filename, so book_id
    # alone yields nothing useful.
    query = {"isbn": isbn, "title": title, "author": author}
    if not any(query.values()):
        with suppress(BookLoreError):
            book = await runtime.client.get(
                f"/api/v1/books/{book_id}", params={"withDescription": "false"}
            )
            meta = book.get("metadata") or {}
            names = _author_names(meta)
            query = {
                "isbn": meta.get("isbn13") or meta.get("isbn10"),
                "title": book.get("title") or meta.get("title"),
                "author": names[0] if names else None,
            }

    token = await runtime.client._ensure_token()
    body = {"bookId": book_id, "providers": provs, **query}

    candidates: list[dict] = []
    try:
        async with runtime.client._http.stream(
            "POST",
            f"/api/v1/books/{book_id}/metadata/prospective",
            json=body,
            headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
            timeout=180.0,
        ) as resp:
            if resp.status_code >= 400:
                detail = (await resp.aread()).decode("utf-8", "replace")
                raise BookLoreError(f"prospective metadata -> {resp.status_code}: {detail}")
            async for line in resp.aiter_lines():
                if line.startswith("data:"):
                    payload = line[len("data:") :].strip()
                    if payload:
                        with suppress(json.JSONDecodeError):
                            candidates.append(json.loads(payload))
    except httpx.TimeoutException as exc:
        raise BookLoreError(
            f"Fetching candidates for book {book_id} timed out — external providers "
            f"can be slow. Try fewer providers or retry."
        ) from exc

    # P0: derive per-provider status. The backend swallows provider errors (emits nothing
    # for a failed provider), so we infer status from result counts, enriched with the
    # provider config when readable (disabled provider, Amazon cookie, field toggles).
    settings = await _app_settings()
    queried = bool(query.get("isbn") or query.get("title"))
    counts: Counter[str] = Counter(str(p) for c in candidates if (p := c.get("provider")))
    provider_status = [_provider_status(p, counts.get(p, 0), settings, queried) for p in provs]

    return {
        "book_id": book_id,
        "query": query,
        "providers": provs,
        "provider_status": provider_status,
        "count": len(candidates),
        "candidates": candidates,
    }


@mcp.tool(annotations={"idempotentHint": True, "title": "Normalize Goodreads IDs"})
async def normalize_goodreads_ids(dry_run: bool = True) -> dict:
    """One-time cleanup: rewrite any stored goodreadsId held in slug form
    ("23463279-designing-data-intensive-applications") to its bare numeric id
    ("23463279"). BookLore persists both forms depending on the fetch path, and writes
    through this MCP are normalized going forward — but pre-existing records aren't.

    Scans the whole library. With dry_run=True (default) it only REPORTS what it would
    change (review first); pass dry_run=False to apply. Each write touches only the
    goodreadsId field (REPLACE_WHEN_PROVIDED, nothing else is altered).
    Returns {scanned, dry_run, to_change|changed:[{book_id,from,to}], failed}.
    """
    books = await _all_books(with_description=True)
    changes: list[dict] = []
    for b in books:
        gid = _meta(b).get("goodreadsId")
        if not isinstance(gid, str) or not gid:
            continue
        norm = _normalize_goodreads_id(gid)
        if norm != gid:
            changes.append({"book_id": b.get("id"), "from": gid, "to": norm})

    if dry_run:
        return {"scanned": len(books), "dry_run": True, "to_change": changes, "failed": []}

    changed: list[dict] = []
    failed: list[dict] = []
    for ch in changes:
        try:
            await _put_metadata(ch["book_id"], {"goodreadsId": ch["to"]})
            changed.append(ch)
        except Exception as exc:
            failed.append({"book_id": ch["book_id"], "error": str(exc)})
    return {"scanned": len(books), "dry_run": False, "changed": changed, "failed": failed}
