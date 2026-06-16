"""BookLore MCP server.

Wraps a self-hosted BookLore instance's REST API (`/api/v1/...`) so Claude can
search your library, read and edit metadata, manage shelves, and track reading
progress. Runs on your machine and talks to your BookLore over HTTP; serves MCP
over HTTP (default) or stdio, selected with MCP_TRANSPORT.

Auth: logs in with BOOKLORE_USERNAME / BOOKLORE_PASSWORD to obtain a JWT access
+ refresh token pair, attaches `Authorization: Bearer`, and refreshes on 401.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal

import httpx
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from log import configure_logging, get_logger

# --- Configuration (from environment) --------------------------------------

BASE_URL = os.environ.get("BOOKLORE_URL", "http://localhost:6060").rstrip("/")
USERNAME = os.environ.get("BOOKLORE_USERNAME")
PASSWORD = os.environ.get("BOOKLORE_PASSWORD")
# Per-request timeout (seconds). Metadata writes regenerate covers server-side and
# can be slow, so default generously; override with BOOKLORE_TIMEOUT.
TIMEOUT = float(os.environ.get("BOOKLORE_TIMEOUT", "120"))
# Retries (with exponential backoff) for transient failures: timeouts and 429/5xx.
RETRIES = int(os.environ.get("BOOKLORE_RETRIES", "2"))
BACKOFF = float(os.environ.get("BOOKLORE_BACKOFF", "0.5"))
# Seconds to cache the full book list (shared by the search/stats/export tools);
# 0 disables caching. Any write to /api/v1/books invalidates it immediately.
CACHE_TTL = float(os.environ.get("BOOKLORE_CACHE_TTL", "10"))
# Max concurrent per-book operations in bulk_update_metadata. Defaults to 1
# (sequential): BookLore stores tags/categories as shared rows with a UNIQUE name,
# so concurrently adding the SAME new tag to several books trips a data-conflict
# (HTTP 400). Raise this only when per-book patches don't share new tags/categories.
BULK_CONCURRENCY = int(os.environ.get("BOOKLORE_BULK_CONCURRENCY", "1"))

log = get_logger("booklore")

ReadStatus = Literal[
    "UNREAD",
    "READING",
    "RE_READING",
    "READ",
    "PARTIALLY_READ",
    "PAUSED",
    "WONT_READ",
    "ABANDONED",
    "UNSET",
]


# --- BookLore HTTP client (JWT login + refresh) -----------------------------


class BookLoreError(ToolError):
    """Raised when the BookLore API returns an error or auth is misconfigured.

    Subclasses FastMCP's ToolError so the message is treated as intentional,
    client-facing detail (and survives if mask_error_details is ever enabled).
    """


# HTTP statuses that warrant a transient retry (rate limit + gateway/unavailable).
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class BookLoreClient:
    """Thin authenticated wrapper over the BookLore REST API.

    Logs in lazily on first use. Transparently refreshes the access token on a
    401, falling back to a full re-login if the refresh token is also stale.
    """

    def __init__(
        self,
        base_url: str,
        username: str | None,
        password: str | None,
        timeout: float = 60.0,
        retries: int = 0,
        backoff: float = 0.5,
        cache_ttl: float = 0.0,
    ):
        self._base_url = base_url
        self._username = username
        self._password = password
        self._timeout = timeout
        self._retries = max(0, retries)
        self._backoff = backoff
        self._cache_ttl = cache_ttl
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        # Serializes login/refresh so concurrent tool calls can't race on the
        # token pair (e.g. two requests 401'ing at once and double-logging-in).
        self._auth_lock = asyncio.Lock()
        # Short-TTL cache of the full book list, keyed by with_description.
        self._books_cache: dict[bool, tuple[float, list[dict]]] = {}

    # -- transport (retry + wrap httpx errors as BookLoreError) --------------

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send a request, retrying transient failures (timeouts, connection
        errors, 429/5xx) with exponential backoff, and translating persistent
        httpx transport/timeout failures into a clean BookLoreError."""
        attempt = 0
        while True:
            try:
                resp = await self._http.request(method, path, **kwargs)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self._retries:
                    attempt += 1
                    log.warning(
                        "transient transport failure, retrying",
                        method=method,
                        path=path,
                        attempt=attempt,
                        error=str(exc),
                    )
                    await asyncio.sleep(self._backoff * (2 ** (attempt - 1)))
                    continue
                if isinstance(exc, httpx.TimeoutException):
                    raise BookLoreError(
                        f"{method} {path} timed out after {self._timeout}s. BookLore can "
                        f"be slow on metadata writes (cover regeneration) — raise "
                        f"BOOKLORE_TIMEOUT."
                    ) from exc
                raise BookLoreError(
                    f"Could not reach BookLore at {self._base_url} ({method} {path}): {exc}"
                ) from exc

            if resp.status_code in _RETRYABLE_STATUS and attempt < self._retries:
                attempt += 1
                log.warning(
                    "transient HTTP error, retrying",
                    method=method,
                    path=path,
                    status=resp.status_code,
                    attempt=attempt,
                )
                await asyncio.sleep(self._backoff * (2 ** (attempt - 1)))
                continue
            return resp

    # -- auth ----------------------------------------------------------------

    async def _login(self) -> None:
        if not self._username or not self._password:
            raise BookLoreError(
                "BOOKLORE_USERNAME and BOOKLORE_PASSWORD must be set in the "
                "environment for the BookLore MCP server to authenticate."
            )
        resp = await self._send(
            "POST",
            "/api/v1/auth/login",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise BookLoreError(f"Login failed ({resp.status_code}): {resp.text}")
        data = resp.json()
        self._access_token = data["accessToken"]
        self._refresh_token = data["refreshToken"]
        log.info("authenticated with booklore", user=self._username)

    async def _refresh(self) -> bool:
        """Try to refresh the access token. Returns True on success."""
        if not self._refresh_token:
            return False
        resp = await self._send(
            "POST", "/api/v1/auth/refresh", json={"refreshToken": self._refresh_token}
        )
        if resp.status_code != 200:
            log.warning("token refresh failed, will re-login", status=resp.status_code)
            return False
        data = resp.json()
        self._access_token = data["accessToken"]
        self._refresh_token = data["refreshToken"]
        log.debug("refreshed access token")
        return True

    async def _ensure_token(self) -> str:
        """Return a valid access token, logging in lazily on first use."""
        if self._access_token is None:
            async with self._auth_lock:
                if self._access_token is None:  # double-checked under the lock
                    await self._login()
        return self._access_token  # type: ignore[return-value]

    async def _reauthenticate(self, stale_token: str | None) -> None:
        """Refresh (or re-login) after a 401, once per stale token.

        `stale_token` is the token whose request just 401'd. If another task
        already rotated it while we waited for the lock, we reuse their result
        instead of logging in a second time.
        """
        async with self._auth_lock:
            if self._access_token != stale_token:
                return
            if not await self._refresh():
                await self._login()

    # -- request with one auth retry ----------------------------------------

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        caller_headers = kwargs.pop("headers", {})
        token = await self._ensure_token()
        headers = {**caller_headers, "Authorization": f"Bearer {token}"}
        resp = await self._send(method, path, headers=headers, **kwargs)

        if resp.status_code == 401:
            # Access token expired — refresh (or re-login) and retry once.
            log.info("access token rejected, re-authenticating", method=method, path=path)
            await self._reauthenticate(token)
            headers = {**caller_headers, "Authorization": f"Bearer {self._access_token}"}
            resp = await self._send(method, path, headers=headers, **kwargs)

        if resp.status_code >= 400:
            log.warning("booklore api error", method=method, path=path, status=resp.status_code)
            raise BookLoreError(f"{method} {path} -> {resp.status_code}: {resp.text}")

        # A successful write to /books may change the cached list — drop it.
        if method != "GET" and path.startswith("/api/v1/books") and self._books_cache:
            self._books_cache.clear()

        log.debug("booklore request", method=method, path=path, status=resp.status_code)
        if resp.status_code == 204 or not resp.content:
            return None
        ctype = resp.headers.get("content-type", "")
        return resp.json() if "application/json" in ctype else resp.text

    async def get(self, path: str, **kw: Any) -> Any:
        return await self.request("GET", path, **kw)

    async def post(self, path: str, **kw: Any) -> Any:
        return await self.request("POST", path, **kw)

    async def put(self, path: str, **kw: Any) -> Any:
        return await self.request("PUT", path, **kw)

    async def delete(self, path: str, **kw: Any) -> Any:
        return await self.request("DELETE", path, **kw)

    async def get_books(self, with_description: bool = False) -> list[dict]:
        """Fetch the full book list, served from a short-TTL cache when enabled.
        The cache is invalidated automatically on any write to /api/v1/books."""
        if self._cache_ttl > 0:
            hit = self._books_cache.get(with_description)
            if hit and time.monotonic() - hit[0] < self._cache_ttl:
                return hit[1]
        books = (
            await self.get(
                "/api/v1/books", params={"withDescription": str(with_description).lower()}
            )
            or []
        )
        if self._cache_ttl > 0:
            self._books_cache[with_description] = (time.monotonic(), books)
        return books

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()


client = BookLoreClient(
    BASE_URL,
    USERNAME,
    PASSWORD,
    timeout=TIMEOUT,
    retries=RETRIES,
    backoff=BACKOFF,
    cache_ttl=CACHE_TTL,
)


# --- Helpers ----------------------------------------------------------------


class BookSummary(BaseModel):
    """Trimmed view of a Book, returned by the list-style read tools."""

    id: int | None = None
    title: str | None = None
    authors: list[str] | None = None
    series: str | None = None
    readStatus: str | None = None
    personalRating: float | None = None
    shelves: list[str] = []


def _summarize_book(book: dict[str, Any]) -> BookSummary:
    """Trim a full Book record down to the fields most useful in a list view."""
    meta = book.get("metadata") or {}
    raw_authors = meta.get("authors")
    authors: list[str] | None = _author_names(meta) if isinstance(raw_authors, list) else None
    shelves = [
        s["name"] for s in (book.get("shelves") or []) if isinstance(s, dict) and s.get("name")
    ]
    return BookSummary(
        id=book.get("id"),
        title=book.get("title") or meta.get("title"),
        authors=authors,
        series=meta.get("seriesName"),
        readStatus=book.get("readStatus"),
        personalRating=book.get("personalRating"),
        shelves=shelves,
    )


class SearchResult(BaseModel):
    """Paginated result envelope for search_books / books_by_author."""

    total: int
    limit: int
    offset: int
    books: list[BookSummary]


def _author_names(meta: dict) -> list[str]:
    """Author display names from a metadata object (authors may be dicts or strings)."""
    names = []
    for a in meta.get("authors") or []:
        name = a.get("name") if isinstance(a, dict) else a
        if name:
            names.append(str(name))
    return names


# Predicates for `missing` filtering — True means the field is empty/absent. `isbn`
# and `description` are sparsely populated upstream; `cover` uses coverUpdatedOn as a
# presence proxy (BookLore doesn't expose a cover URL on the book record).
_MISSING_CHECKS = {
    "tags": lambda m: not m.get("tags"),
    "categories": lambda m: not m.get("categories"),
    "authors": lambda m: not _author_names(m),
    "description": lambda m: not m.get("description"),
    "isbn": lambda m: not (m.get("isbn10") or m.get("isbn13")),
    "series": lambda m: not m.get("seriesName"),
    "cover": lambda m: not m.get("coverUpdatedOn"),
}

_SORT_KEYS = {
    "title": lambda b: (b.get("title") or (b.get("metadata") or {}).get("title") or "").lower(),
    "addedOn": lambda b: b.get("addedOn") or "",
    "rating": lambda b: b["personalRating"] if b.get("personalRating") is not None else -1,
    "readStatus": lambda b: b.get("readStatus") or "",
}


def _matches_query(book: dict, q: str) -> bool:
    """Case-insensitive substring match over title/subtitle/isbn/author names."""
    meta = book.get("metadata") or {}
    parts = [
        book.get("title") or meta.get("title"),
        meta.get("subtitle"),
        meta.get("isbn10"),
        meta.get("isbn13"),
        *_author_names(meta),
    ]
    return q in " ".join(str(x) for x in parts if x).lower()


def _matches_filters(book: dict, filters: dict) -> bool:
    """Apply a search_books `filters` object to a raw book record."""
    meta = book.get("metadata") or {}

    if tags := filters.get("tags"):
        have = {t.lower() for t in (meta.get("tags") or [])}
        want = {t.lower() for t in tags}
        if filters.get("tags_mode") == "all":
            if not want.issubset(have):
                return False
        elif not (want & have):
            return False

    if cats := filters.get("categories"):
        have = {c.lower() for c in (meta.get("categories") or [])}
        if not ({c.lower() for c in cats} & have):
            return False

    if authors := filters.get("authors"):
        names = [n.lower() for n in _author_names(meta)]
        if not any(q.lower() in n for q in authors for n in names):
            return False

    if shelf_ids := filters.get("shelf_ids"):
        have_ids = {s.get("id") for s in (book.get("shelves") or []) if isinstance(s, dict)}
        if not (set(shelf_ids) & have_ids):
            return False

    if (rs := filters.get("read_status")) and (book.get("readStatus") or "UNSET") != rs:
        return False

    if (libs := filters.get("library_ids")) and book.get("libraryId") not in set(libs):
        return False

    # `missing`: keep only books for which *every* listed field is empty.
    for field in filters.get("missing") or []:
        check = _MISSING_CHECKS.get(field)
        if check and not check(meta):
            return False

    return True


def _merge_list(current: list | None, add: list | None = None, remove: list | None = None) -> list:
    """Case-insensitive add/remove on a string list, preserving order and casing."""
    remove_lower = {str(r).lower() for r in (remove or [])}
    result = [x for x in (current or []) if str(x).lower() not in remove_lower]
    have = {str(x).lower() for x in result}
    for a in add or []:
        if str(a).lower() not in have:
            result.append(a)
            have.add(str(a).lower())
    return result


async def _all_books(with_description: bool = False) -> list[dict]:
    return await client.get_books(with_description)


async def _put_metadata(book_id: int, meta_patch: dict) -> Any:
    """PUT a partial metadata object, touching ONLY the supplied fields.

    Uses REPLACE_WHEN_PROVIDED: BookLore writes just the keys present in the
    payload and leaves every other field untouched. (REPLACE_ALL would null out
    every field we omit — title, authors, description, ISBNs — so it must never
    be used with a partial patch.) `clearFlags` must be present or the API 500s.
    """
    return await client.put(
        f"/api/v1/books/{book_id}/metadata",
        params={"mergeCategories": "false", "replaceMode": "REPLACE_WHEN_PROVIDED"},
        json={"metadata": meta_patch, "clearFlags": {}},
    )


async def _apply_patch(book_id: int, patch: dict) -> dict:
    """Apply an additive tags/categories patch to one book; return its fresh record.

    `patch` is {"tags": {"add": [...], "remove": [...]}, "categories": {...}}.
    """
    book = await client.get(f"/api/v1/books/{book_id}", params={"withDescription": "false"})
    meta = book.get("metadata") or {}
    meta_patch: dict[str, Any] = {}
    for field in ("tags", "categories"):
        if field in patch:
            ops = patch[field] or {}
            meta_patch[field] = _merge_list(meta.get(field), ops.get("add"), ops.get("remove"))
    if not meta_patch:
        return book
    await _put_metadata(book_id, meta_patch)
    return await client.get(f"/api/v1/books/{book_id}", params={"withDescription": "false"})


# --- MCP server -------------------------------------------------------------


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
    """Close the shared HTTP client when the server shuts down."""
    try:
        yield
    finally:
        await client.close()


mcp = FastMCP(
    name="booklore",
    lifespan=lifespan,
    instructions=(
        "Tools for a self-hosted BookLore library. Book and shelf IDs are not "
        "guessable — use list_books / list_shelves to discover them before "
        "calling tools that take an ID. list_books returns trimmed summaries; "
        "call get_book for the full record including metadata. To set reading "
        "progress you need a book_file_id — read it from get_book's primaryFile."
    ),
)


# ---- Read tools ------------------------------------------------------------


@mcp.tool(
    annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List / search books"}
)
async def list_books(query: str = "", limit: int = 25) -> list[BookSummary]:
    """List books in the library, optionally filtered by a keyword.

    `query` is matched (case-insensitive substring) against title and author
    names client-side; leave empty to list everything. Returns trimmed summaries
    (id, title, authors, series, readStatus, personalRating, shelves) capped at
    `limit`. Call get_book for full details on one book.
    """
    books = await client.get("/api/v1/books", params={"withDescription": "false"}) or []
    summaries = [_summarize_book(b) for b in books]
    if query:
        q = query.lower()

        def matches(s: BookSummary) -> bool:
            hay = " ".join(str(x) for x in [s.title, *(s.authors or [])] if x).lower()
            return q in hay

        summaries = [s for s in summaries if matches(s)]
    return summaries[:limit]


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Get book details"})
async def get_book(book_id: int, with_description: bool = True) -> dict:
    """Fetch a single book's full record by ID, including its metadata.

    Use list_books first if you don't know the ID. Set with_description=False to
    omit the (potentially long) description field.
    """
    return await client.get(
        f"/api/v1/books/{book_id}",
        params={"withDescription": str(with_description).lower()},
    )


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List shelves"})
async def list_shelves() -> list[dict]:
    """List all shelves (id, name, icon, public flag). Use get_shelf_books to see
    the books on a given shelf."""
    return await client.get("/api/v1/shelves") or []


@mcp.tool(
    annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Get books on a shelf"}
)
async def get_shelf_books(shelf_id: int) -> list[BookSummary]:
    """List the books on a shelf, as trimmed summaries. Get shelf IDs from
    list_shelves."""
    books = await client.get(f"/api/v1/shelves/{shelf_id}/books") or []
    return [_summarize_book(b) for b in books]


@mcp.tool(
    annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Get recommendations"}
)
async def get_recommendations(book_id: int, limit: int = 10) -> list[dict]:
    """Get books recommended as similar to the given book (max 25)."""
    return (
        await client.get(
            f"/api/v1/books/{book_id}/recommendations",
            params={"limit": max(1, min(limit, 25))},
        )
        or []
    )


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
    return await client.post("/api/v1/metadata/isbn-lookup", json={"isbn": isbn})


# ---- Write tools -----------------------------------------------------------


@mcp.tool(annotations={"title": "Update book metadata"})
async def update_book_metadata(
    book_id: int,
    metadata: dict,
    replace_mode: Literal["REPLACE_WHEN_PROVIDED", "REPLACE_MISSING", "REPLACE_ALL"] = (
        "REPLACE_WHEN_PROVIDED"
    ),
    merge_categories: bool = False,
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

    `authors` is a list of name strings. Returns the full updated metadata record.
    """
    # `clearFlags` must be present: the server's @Builder.Default isn't applied
    # on JSON deserialization, so omitting it leaves it null and the API 500s.
    return await client.put(
        f"/api/v1/books/{book_id}/metadata",
        params={"mergeCategories": str(merge_categories).lower(), "replaceMode": replace_mode},
        json={"metadata": metadata, "clearFlags": {}},
    )


@mcp.tool(annotations={"idempotentHint": True, "title": "Set read status"})
async def set_read_status(book_ids: list[int], status: ReadStatus) -> list[dict]:
    """Set the reading status for one or more books (e.g. READING, READ, UNREAD).
    Returns per-book update results."""
    return await client.post("/api/v1/books/status", json={"bookIds": book_ids, "status": status})


@mcp.tool(annotations={"idempotentHint": True, "title": "Set personal rating"})
async def set_personal_rating(book_ids: list[int], rating: int) -> list[dict]:
    """Set your personal rating for one or more books. Rating uses BookLore's
    configured scale (commonly 0-10 in half-star steps shown as 0-5 stars in the
    UI). Returns per-book update results."""
    return await client.put(
        "/api/v1/books/personal-rating", json={"ids": book_ids, "rating": rating}
    )


@mcp.tool(annotations={"idempotentHint": True, "title": "Assign / unassign shelves"})
async def assign_shelves(
    book_ids: list[int],
    add_shelf_ids: list[int] | None = None,
    remove_shelf_ids: list[int] | None = None,
) -> list[dict]:
    """Add and/or remove books from shelves in one call. Get shelf IDs from
    list_shelves and book IDs from list_books. Returns the affected books."""
    return await client.post(
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
    return await client.post("/api/v1/shelves", json=body)


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True, "title": "Delete shelf"})
async def delete_shelf(shelf_id: int) -> str:
    """Delete a shelf by ID. Does NOT delete the books on it. This cannot be
    undone — confirm the shelf ID with list_shelves first."""
    await client.delete(f"/api/v1/shelves/{shelf_id}")
    return f"Deleted shelf {shelf_id}."


@mcp.tool(annotations={"idempotentHint": True, "title": "Set reading progress"})
async def set_reading_progress(
    book_id: int,
    book_file_id: int,
    progress_percent: float,
    position_href: str | None = None,
    position_data: str | None = None,
    date_finished: str | None = None,
) -> str:
    """Record reading progress for a book file.

    `book_file_id` is the file you're tracking — get it from get_book's
    `primaryFile.id` (or `alternativeFormats`). `progress_percent` is 0-100.
    `position_href` / `position_data` are optional format-specific resume
    pointers (e.g. an EPUB CFI). Pass `date_finished` as an ISO-8601 timestamp
    (e.g. "2026-06-09T12:00:00Z") to also mark the book finished.
    """
    body: dict[str, Any] = {
        "bookId": book_id,
        "fileProgress": {
            "bookFileId": book_file_id,
            "progressPercent": progress_percent,
            "positionHref": position_href,
            "positionData": position_data,
        },
    }
    if date_finished:
        body["dateFinished"] = date_finished
    await client.post("/api/v1/books/progress", json=body)
    return f"Set reading progress for book {book_id} to {progress_percent}%."


@mcp.tool(
    annotations={"destructiveHint": True, "idempotentHint": True, "title": "Reset reading progress"}
)
async def reset_progress(
    book_ids: list[int],
    source: Literal["BOOKLORE", "KOREADER", "KOBO"] = "BOOKLORE",
) -> list[dict]:
    """Clear reading progress for one or more books (max 500). `source` selects
    which progress to wipe: BOOKLORE (the built-in reader), KOREADER, or KOBO.
    This discards saved position — it cannot be undone."""
    return await client.post("/api/v1/books/reset-progress", params={"type": source}, json=book_ids)


# ---- Search & discovery ----------------------------------------------------


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Search books"})
async def search_books(
    query: str = "",
    filters: dict | None = None,
    sort: Literal["title", "addedOn", "rating", "readStatus"] = "title",
    order: Literal["asc", "desc"] = "asc",
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    """Search and filter the library server-side-style, returning a paginated page.

    `query` is a case-insensitive substring over title/subtitle/isbn/author names.
    `filters` is an optional object:
      - tags: [str], tags_mode: "any"|"all" (default "any")
      - categories: [str] (any), authors: [str] (any, substring)
      - shelf_ids: [int], library_ids: [int]
      - read_status: "UNSET"|"READING"|"READ"|"UNREAD"|… (sparse upstream)
      - missing: subset of ["tags","categories","authors","description","isbn",
        "series","cover"] — keeps only books where ALL listed fields are empty.
        This is the "find everything still un-enriched" filter.
    Returns {total, limit, offset, books:[summaries]}. `limit` is capped at 200.
    """
    filters = filters or {}
    need_desc = "description" in (filters.get("missing") or [])
    books = await _all_books(with_description=need_desc)

    if query:
        ql = query.lower()
        books = [b for b in books if _matches_query(b, ql)]
    if filters:
        books = [b for b in books if _matches_filters(b, filters)]

    books.sort(key=_SORT_KEYS.get(sort, _SORT_KEYS["title"]), reverse=(order == "desc"))

    total = len(books)
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    page = books[offset : offset + limit]
    return SearchResult(
        total=total, limit=limit, offset=offset, books=[_summarize_book(b) for b in page]
    )


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List authors"})
async def list_authors(query: str = "", limit: int = 100, offset: int = 0) -> dict:
    """List authors (from BookLore's authors index) with per-author book counts.
    Optionally filter by a case-insensitive substring `query`."""
    authors = await client.get("/api/v1/authors") or []
    if query:
        ql = query.lower()
        authors = [a for a in authors if ql in (a.get("name") or "").lower()]
    authors.sort(key=lambda a: (a.get("name") or "").lower())
    page = authors[max(0, offset) : max(0, offset) + max(1, limit)]
    return {
        "total": len(authors),
        "authors": [
            {"id": a.get("id"), "name": a.get("name"), "book_count": a.get("bookCount")}
            for a in page
        ],
    }


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Books by author"})
async def books_by_author(author: str, limit: int = 50, offset: int = 0) -> SearchResult:
    """List books by an author (case-insensitive exact match on the author name)."""
    al = author.lower()
    matched = [
        b
        for b in await _all_books()
        if any(al == n.lower() for n in _author_names(b.get("metadata") or {}))
    ]
    total = len(matched)
    limit = max(1, limit)
    offset = max(0, offset)
    page = matched[offset : offset + limit]
    return SearchResult(
        total=total, limit=limit, offset=offset, books=[_summarize_book(b) for b in page]
    )


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Reading stats"})
async def get_reading_stats() -> dict:
    """Library-wide reading stats: counts by read status and average personal rating.

    Note: BookLore's list endpoint exposes readStatus / personalRating sparsely, so
    most books report as UNSET / unrated here."""
    books = await _all_books()
    by_status = Counter((b.get("readStatus") or "UNSET") for b in books)
    ratings = [b["personalRating"] for b in books if b.get("personalRating") is not None]
    avg = round(sum(ratings) / len(ratings), 2) if ratings else None
    return {
        "total": len(books),
        "by_status": dict(by_status),
        "rated_count": len(ratings),
        "avg_rating": avg,
    }


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Export library"})
async def export_library(
    format: Literal["json", "csv"] = "json", shelf_id: int | None = None
) -> dict:
    """Export the library (or one shelf) as JSON or CSV. Returns
    {format, count, content} where `content` is the serialized text."""
    if shelf_id is not None:
        books = await client.get(f"/api/v1/shelves/{shelf_id}/books") or []
    else:
        books = await _all_books()
    summaries = [_summarize_book(b) for b in books]

    if format == "json":
        content = json.dumps([s.model_dump() for s in summaries], ensure_ascii=False, indent=2)
    else:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            ["id", "title", "authors", "series", "readStatus", "personalRating", "shelves"]
        )
        for s in summaries:
            writer.writerow(
                [
                    s.id,
                    s.title,
                    "; ".join(s.authors or []),
                    s.series,
                    s.readStatus,
                    s.personalRating,
                    "; ".join(s.shelves),
                ]
            )
        content = buf.getvalue()
    return {"format": format, "count": len(summaries), "content": content}


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
        health = (await client._http.get("/api/v1/healthcheck")).json()
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
        books = await client.get("/api/v1/books", params={"withDescription": "false"})
    except BookLoreError as exc:
        result["error"] = str(exc)
        return result
    result.update(authenticated=True, user=USERNAME, book_count=len(books or []))
    with suppress(BookLoreError):
        result["library_count"] = len(await client.get("/api/v1/libraries") or [])
    return result


# ---- Bulk & additive writes ------------------------------------------------


@mcp.tool(annotations={"title": "Bulk update metadata"})
async def bulk_update_metadata(
    book_ids: list[int] | None = None,
    patch: dict | None = None,
    items: list[dict] | None = None,
) -> dict:
    """Apply additive tag/category patches to many books in one call.

    Either pass `book_ids` + a single `patch` (applied to all), or `items`
    (`[{"book_id": int, "patch": {...}}]`) for per-book patches — not both. A
    `patch` is {"tags": {"add": [...], "remove": [...]}, "categories": {...}}.
    Partial success is reported per book; one bad id never fails the batch.
    Returns {updated:[ids], failed:[{book_id,error}], books:[post-write records]}.
    """
    if items and (book_ids or patch):
        raise BookLoreError("Pass either book_ids+patch OR items, not both.")
    if not items and not (book_ids and patch):
        raise BookLoreError("Provide book_ids+patch, or items.")

    work = items if items else [{"book_id": bid, "patch": patch} for bid in (book_ids or [])]
    semaphore = asyncio.Semaphore(BULK_CONCURRENCY)

    async def _one(entry: dict) -> tuple[Any, Any, dict | None]:
        bid = entry.get("book_id")
        async with semaphore:
            try:
                if not isinstance(bid, int):
                    raise BookLoreError("each item needs an integer 'book_id'")
                return bid, await _apply_patch(bid, entry.get("patch") or {}), None
            except Exception as exc:
                return bid, None, {"book_id": bid, "error": str(exc)}

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
    return await client.put(
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
    Lubimyczytac, Ranobedb, Audible (defaults to a few popular ones). `isbn`/`title`/
    `author` optionally override the search terms. Returns the raw candidates from
    each provider; review them, then apply a chosen one with update_book_metadata
    (lock curated fields first with set_field_locks to protect them).
    """
    provs = providers or ["Google", "GoodReads", "Amazon"]
    token = await client._ensure_token()
    body = {"bookId": book_id, "providers": provs, "isbn": isbn, "title": title, "author": author}

    candidates: list[dict] = []
    try:
        async with client._http.stream(
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
    return {
        "book_id": book_id,
        "providers": provs,
        "count": len(candidates),
        "candidates": candidates,
    }


@mcp.tool(annotations={"idempotentHint": True, "title": "Add books to shelves"})
async def add_to_shelves(book_ids: list[int], shelf_ids: list[int]) -> list[dict]:
    """Append books to shelves without disturbing their other shelves (idempotent).
    The additive counterpart to assign_shelves. Returns the affected books."""
    return await client.post(
        "/api/v1/books/shelves",
        json={"bookIds": book_ids, "shelvesToAssign": shelf_ids, "shelvesToUnassign": []},
    )


@mcp.tool(annotations={"idempotentHint": True, "title": "Remove books from shelves"})
async def remove_from_shelves(book_ids: list[int], shelf_ids: list[int]) -> list[dict]:
    """Remove books from shelves without disturbing their other shelves (idempotent).
    Returns the affected books."""
    return await client.post(
        "/api/v1/books/shelves",
        json={"bookIds": book_ids, "shelvesToAssign": [], "shelvesToUnassign": shelf_ids},
    )


def main() -> None:
    """Console entry point. Transport is selectable via env: defaults to a
    long-lived HTTP server; set MCP_TRANSPORT=stdio for a local process that an
    MCP host (Claude Desktop/Code) launches directly."""
    configure_logging()
    transport = os.environ.get("MCP_TRANSPORT", "http")
    if transport == "stdio":
        log.info("starting booklore mcp server", transport="stdio", booklore_url=BASE_URL)
        mcp.run()  # stdio
    else:
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_PORT", "8000"))
        path = os.environ.get("MCP_PATH", "/mcp")
        log.info(
            "starting booklore mcp server",
            transport="http",
            host=host,
            port=port,
            path=path,
            booklore_url=BASE_URL,
        )
        mcp.run(transport="http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
