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
import re
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal, get_args

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
    "isbn10": lambda m: not m.get("isbn10"),
    "isbn13": lambda m: not m.get("isbn13"),
    "series": lambda m: not m.get("seriesName"),
    "cover": lambda m: not m.get("coverUpdatedOn"),
    "asin": lambda m: not m.get("asin"),
    "goodreadsId": lambda m: not m.get("goodreadsId"),
    "googleId": lambda m: not m.get("googleId"),
    "hardcoverId": lambda m: not m.get("hardcoverId"),
    "amazonRating": lambda m: not m.get("amazonRating"),
    "goodreadsRating": lambda m: not m.get("goodreadsRating"),
    "publisher": lambda m: not m.get("publisher"),
    "language": lambda m: not m.get("language"),
    "pageCount": lambda m: not m.get("pageCount"),
}
# Of the missing-checks, those whose field BookLore strips from the plain list response.
_MISSING_FULL_ONLY = {"asin", "goodreadsId", "googleId", "hardcoverId", "description"}

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

    # `missing`: filter on empty fields. mode "all" (default) keeps a book only when every
    # listed field is empty; mode "any" keeps it when at least one listed field is empty.
    if missing := filters.get("missing"):
        flags = [_MISSING_CHECKS[f](meta) for f in missing if f in _MISSING_CHECKS]
        if flags:
            if filters.get("missing_mode") == "any":
                if not any(flags):
                    return False
            elif not all(flags):
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

# Fields BookLore's clearFlags map accepts on PUT .../metadata. Setting a flag true nulls
# that field regardless of replaceMode — the only way to clear ONE field without
# REPLACE_ALL wiping the whole record. (From the backend's MetadataClearFlags class.)
_CLEARABLE_FIELDS = {
    "title",
    "subtitle",
    "publisher",
    "publishedDate",
    "description",
    "seriesName",
    "seriesNumber",
    "seriesTotal",
    "isbn13",
    "isbn10",
    "asin",
    "goodreadsId",
    "comicvineId",
    "hardcoverId",
    "hardcoverBookId",
    "googleId",
    "pageCount",
    "language",
    "amazonRating",
    "amazonReviewCount",
    "goodreadsRating",
    "goodreadsReviewCount",
    "hardcoverRating",
    "hardcoverReviewCount",
    "lubimyczytacId",
    "lubimyczytacRating",
    "ranobedbId",
    "ranobedbRating",
    "audibleId",
    "audibleRating",
    "audibleReviewCount",
    "authors",
    "categories",
    "moods",
    "tags",
    "cover",
    "reviews",
    "narrator",
    "abridged",
    "ageRating",
    "contentRating",
}

# Goodreads ids arrive both bare ("171712768") and slugged
# ("23463279-designing-data-intensive-applications"); BookLore stores either verbatim.
_GOODREADS_ID_RE = re.compile(r"^\s*(\d+)(?:-.*)?\s*$")


def _normalize_goodreads_id(value: Any) -> Any:
    """Reduce a Goodreads id to its bare numeric form, leaving non-numeric values as-is."""
    if not isinstance(value, str):
        return value
    m = _GOODREADS_ID_RE.match(value)
    return m.group(1) if m else value.strip()


def _normalize_metadata(meta: dict) -> dict:
    """Copy of a metadata patch with identifier fields normalized on write."""
    out = dict(meta)
    if out.get("goodreadsId") is not None:
        out["goodreadsId"] = _normalize_goodreads_id(out["goodreadsId"])
    return out


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
        settings = await client.get("/api/v1/settings")
        if isinstance(settings, dict):
            return settings
    return None


def _meta(book: dict) -> dict:
    return book.get("metadata") or {}


def _shelf_names(book: dict) -> list[str]:
    return [s["name"] for s in (book.get("shelves") or []) if isinstance(s, dict) and s.get("name")]


# Flat field extractors for export_library's `fields` projection. Fields beyond the plain
# list view (asin, goodreadsId, googleId, hardcoverId, subtitle, description) are stripped
# by BookLore's list endpoint, so selecting them forces a withDescription fetch.
_EXPORT_FIELDS: dict[str, Any] = {
    "id": lambda b: b.get("id"),
    "title": lambda b: b.get("title") or _meta(b).get("title"),
    "subtitle": lambda b: _meta(b).get("subtitle"),
    "authors": lambda b: _author_names(_meta(b)),
    "series": lambda b: _meta(b).get("seriesName"),
    "seriesNumber": lambda b: _meta(b).get("seriesNumber"),
    "publisher": lambda b: _meta(b).get("publisher"),
    "publishedDate": lambda b: _meta(b).get("publishedDate"),
    "language": lambda b: _meta(b).get("language"),
    "pageCount": lambda b: _meta(b).get("pageCount"),
    "isbn10": lambda b: _meta(b).get("isbn10"),
    "isbn13": lambda b: _meta(b).get("isbn13"),
    "asin": lambda b: _meta(b).get("asin"),
    "goodreadsId": lambda b: _meta(b).get("goodreadsId"),
    "googleId": lambda b: _meta(b).get("googleId"),
    "hardcoverId": lambda b: _meta(b).get("hardcoverId"),
    "amazonRating": lambda b: _meta(b).get("amazonRating"),
    "goodreadsRating": lambda b: _meta(b).get("goodreadsRating"),
    "personalRating": lambda b: b.get("personalRating"),
    "readStatus": lambda b: b.get("readStatus"),
    "tags": lambda b: list(_meta(b).get("tags") or []),
    "categories": lambda b: list(_meta(b).get("categories") or []),
    "description": lambda b: _meta(b).get("description"),
    "shelves": _shelf_names,
}

# Export/missing fields BookLore omits from the plain list response (need withDescription).
_FULL_ONLY_FIELDS = {"asin", "goodreadsId", "googleId", "hardcoverId", "subtitle", "description"}


async def _all_books(with_description: bool = False) -> list[dict]:
    return await client.get_books(with_description)


async def _put_metadata(
    book_id: int, meta_patch: dict, clear_fields: list[str] | None = None
) -> Any:
    """PUT a partial metadata object, touching ONLY the supplied fields.

    Uses REPLACE_WHEN_PROVIDED: BookLore writes just the keys present in the
    payload and leaves every other field untouched. (REPLACE_ALL would null out
    every field we omit — title, authors, description, ISBNs — so it must never
    be used with a partial patch.) `clearFlags` must be present or the API 500s.

    `clear_fields` are field names to null out: BookLore honours a true clearFlag
    regardless of replace mode, so it's the one way to clear a single field without
    REPLACE_ALL wiping the rest.
    """
    clear_flags = {f: True for f in (clear_fields or [])}
    return await client.put(
        f"/api/v1/books/{book_id}/metadata",
        params={"mergeCategories": "false", "replaceMode": "REPLACE_WHEN_PROVIDED"},
        json={"metadata": meta_patch, "clearFlags": clear_flags},
    )


def _validate_clear_fields(clear_fields: list[str] | None) -> None:
    """Reject clearFlag names BookLore won't accept, with a helpful message."""
    bad = sorted(set(clear_fields or []) - _CLEARABLE_FIELDS)
    if bad:
        raise BookLoreError(
            f"Cannot clear unknown field(s): {', '.join(bad)}. "
            f"Clearable fields: {', '.join(sorted(_CLEARABLE_FIELDS))}."
        )


async def _apply_patch(book_id: int, patch: dict) -> dict:
    """Apply a per-book patch to one book; return its fresh record.

    `patch` keys:
      - tags / categories: {"add": [...], "remove": [...]} — additive merges.
      - metadata: {field: value, ...} — arbitrary BookMetadata fields written
        with REPLACE_WHEN_PROVIDED (only the keys you pass; identifiers normalized).
      - clear_fields: [field, ...] — fields to null out (see _put_metadata).
    """
    clear_fields = patch.get("clear_fields") or []
    _validate_clear_fields(clear_fields)
    book = await client.get(f"/api/v1/books/{book_id}", params={"withDescription": "false"})
    meta = book.get("metadata") or {}
    meta_patch: dict[str, Any] = {}
    for field in ("tags", "categories"):
        if field in patch:
            ops = patch[field] or {}
            meta_patch[field] = _merge_list(meta.get(field), ops.get("add"), ops.get("remove"))
    if extra := patch.get("metadata"):
        meta_patch.update(_normalize_metadata(extra))
    if not meta_patch and not clear_fields:
        return book
    await _put_metadata(book_id, meta_patch, clear_fields)
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
    return await client.put(
        f"/api/v1/books/{book_id}/metadata",
        params={"mergeCategories": str(merge_categories).lower(), "replaceMode": replace_mode},
        json={"metadata": metadata, "clearFlags": clear_flags},
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
        "isbn10","isbn13","series","cover","asin","goodreadsId","googleId",
        "hardcoverId","amazonRating","goodreadsRating","publisher","language",
        "pageCount"] — fields to treat as empty.
      - missing_mode: "all" (default) keeps books where ALL listed fields are
        empty; "any" keeps books missing AT LEAST ONE — better for enrichment
        sweeps ("anything still lacking an id/rating").
    Returns {total, limit, offset, books:[summaries]}. `limit` is capped at 200.
    """
    filters = filters or {}
    missing = filters.get("missing") or []
    # Identifier fields (asin/goodreadsId/…) and description are stripped from the plain
    # list response — fetch full metadata when the query depends on one of them.
    need_desc = any(f in _MISSING_FULL_ONLY for f in missing)
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
    format: Literal["json", "csv"] = "json",
    shelf_id: int | None = None,
    fields: list[str] | None = None,
    full: bool = False,
    offset: int = 0,
    limit: int | None = None,
) -> dict:
    """Export the library (or one shelf) as JSON or CSV.

    By default returns the trimmed summary (id/title/authors/series/readStatus/
    personalRating/shelves). To audit metadata completeness, pass `fields` to
    project a specific set, or `full=True` for every field. Selectable fields:
    id, title, subtitle, authors, series, seriesNumber, publisher, publishedDate,
    language, pageCount, isbn10, isbn13, asin, goodreadsId, googleId, hardcoverId,
    amazonRating, goodreadsRating, personalRating, readStatus, tags, categories,
    description, shelves. Identifier fields (asin/goodreadsId/…) and description are
    fetched on demand.

    A full-library export easily exceeds the response token limit, so: prefer `csv`
    (far more compact than JSON), keep `fields` narrow, and page with `offset`/`limit`
    (the response reports `total` so you know how many pages remain). JSON output is
    compact (no indentation). Returns {format, total, offset, limit, returned, fields,
    content}.
    """
    if fields:
        bad = sorted(set(fields) - set(_EXPORT_FIELDS))
        if bad:
            raise BookLoreError(
                f"Unknown export field(s): {', '.join(bad)}. "
                f"Available: {', '.join(_EXPORT_FIELDS)}."
            )
        cols = list(fields)
    elif full:
        cols = list(_EXPORT_FIELDS)
    else:
        cols = ["id", "title", "authors", "series", "readStatus", "personalRating", "shelves"]

    need_desc = any(c in _FULL_ONLY_FIELDS for c in cols)
    if shelf_id is not None:
        books = await client.get(f"/api/v1/shelves/{shelf_id}/books") or []
        if need_desc:  # shelf endpoint returns list-trimmed records; hydrate by id
            full_by_id = {b.get("id"): b for b in await _all_books(with_description=True)}
            books = [full_by_id.get(b.get("id"), b) for b in books]
    else:
        books = await _all_books(with_description=need_desc)

    total = len(books)
    offset = max(0, offset)
    page = books[offset : offset + limit] if limit is not None else books[offset:]
    rows = [{c: _EXPORT_FIELDS[c](b) for c in cols} for b in page]

    if format == "json":
        content = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    else:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(cols)
        for row in rows:
            writer.writerow(
                ["; ".join(str(x) for x in v) if isinstance(v, list) else v for v in row.values()]
            )
        content = buf.getvalue()
    return {
        "format": format,
        "total": total,
        "offset": offset,
        "limit": limit,
        "returned": len(rows),
        "fields": cols,
        "content": content,
    }


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
            book = await client.get(f"/api/v1/books/{book_id}", params={"withDescription": "false"})
            meta = book.get("metadata") or {}
            names = _author_names(meta)
            query = {
                "isbn": meta.get("isbn13") or meta.get("isbn10"),
                "title": book.get("title") or meta.get("title"),
                "author": names[0] if names else None,
            }

    token = await client._ensure_token()
    body = {"bookId": book_id, "providers": provs, **query}

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


@mcp.tool(annotations={"idempotentHint": True, "title": "Bulk set read status"})
async def bulk_set_read_status(items: list[dict]) -> dict:
    """Set per-book reading status in bulk — e.g. importing a Goodreads CSV where each
    book has its own status. `items` is [{"book_id": int, "status": ReadStatus}], with
    status one of UNREAD/READING/RE_READING/READ/PARTIALLY_READ/PAUSED/WONT_READ/
    ABANDONED/UNSET. Books are grouped by status and sent in ONE call per distinct
    status (not one per book). Returns {updated, by_status:{status:count}, failed}.
    """
    valid = set(get_args(ReadStatus))
    groups: dict[str, list[int]] = {}
    failed: list[dict] = []
    for entry in items:
        bid = entry.get("book_id")
        status = entry.get("status")
        if not isinstance(bid, int) or not isinstance(status, str) or status not in valid:
            failed.append({"book_id": bid, "error": f"invalid book_id/status: {entry}"})
            continue
        groups.setdefault(status, []).append(bid)

    updated: list[int] = []
    by_status: dict[str, int] = {}
    for status, ids in groups.items():
        await client.post("/api/v1/books/status", json={"bookIds": ids, "status": status})
        updated.extend(ids)
        by_status[status] = len(ids)
    return {"updated": updated, "by_status": by_status, "failed": failed}


# Duplicate-detection signal presets (BookLore exposes no presets server-side).
_DUP_SIGNALS = (
    "matchByIsbn",
    "matchByExternalId",
    "matchByTitleAuthor",
    "matchByDirectory",
    "matchByFilename",
)
_DUP_PRESETS = {
    "strict": {"matchByIsbn", "matchByExternalId"},
    "balanced": {"matchByIsbn", "matchByExternalId", "matchByTitleAuthor"},
    "aggressive": set(_DUP_SIGNALS),
}


@mcp.tool(annotations={"readOnlyHint": True, "title": "Find duplicates"})
async def find_duplicates(
    library_id: int | None = None,
    preset: Literal["strict", "balanced", "aggressive"] = "balanced",
    signals: dict | None = None,
) -> dict:
    """Find duplicate books using BookLore's native detection — read-only, merges nothing.

    Detection is per-library; omit `library_id` to scan every library. `preset` picks
    the signal set: strict (ISBN + external id), balanced (+ title/author), aggressive
    (+ same-directory + filename). `signals` overrides individual flags, e.g.
    {"matchByFilename": true}. Each group reports the suggested merge target, the match
    reason, the books, and `distinct_isbns` (true ⇒ the books carry different ISBNs, so
    likely separate editions rather than true duplicates).
    Returns {libraries_scanned, group_count, groups}.
    """
    flags = {s: (s in _DUP_PRESETS[preset]) for s in _DUP_SIGNALS}
    if signals:
        bad = sorted(set(signals) - set(_DUP_SIGNALS))
        if bad:
            raise BookLoreError(
                f"Unknown duplicate signal(s): {', '.join(bad)}. Valid: {', '.join(_DUP_SIGNALS)}."
            )
        flags.update({k: bool(v) for k, v in signals.items()})

    if library_id is not None:
        library_ids: list[int] = [library_id]
    else:
        libs = await client.get("/api/v1/libraries") or []
        library_ids = [
            lib["id"] for lib in libs if isinstance(lib, dict) and lib.get("id") is not None
        ]

    groups: list[dict] = []
    for lib_id in library_ids:
        result = (
            await client.post("/api/v1/books/duplicates", json={"libraryId": lib_id, **flags}) or []
        )
        for grp in result:
            books = grp.get("books") or []
            isbns = {(_meta(b).get("isbn13") or _meta(b).get("isbn10")) for b in books}
            isbns.discard(None)
            groups.append(
                {
                    "library_id": lib_id,
                    "match_reason": grp.get("matchReason"),
                    "suggested_target_book_id": grp.get("suggestedTargetBookId"),
                    "distinct_isbns": len(isbns) > 1,
                    "books": [
                        {
                            "id": b.get("id"),
                            "title": b.get("title") or _meta(b).get("title"),
                            "authors": _author_names(_meta(b)),
                            "isbn13": _meta(b).get("isbn13"),
                            "isbn10": _meta(b).get("isbn10"),
                        }
                        for b in books
                    ],
                }
            )
    return {"libraries_scanned": len(library_ids), "group_count": len(groups), "groups": groups}


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
