"""BookOrbit MCP server.

Wraps a self-hosted BookOrbit instance's REST API (`/api/v1/...`) so Claude can
search your library, read and edit metadata, manage collections, and track reading
progress. Runs on your machine and talks to your BookOrbit over HTTP; serves MCP
over HTTP (default) or stdio, selected with MCP_TRANSPORT.

Auth: a BookOrbit API key (BOOKORBIT_API_KEY), sent as `X-Api-Key` on every
request. Create one under Settings > You > API keys.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import os
import re
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, Literal, get_args

import httpx
from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel

from log import configure_logging, get_logger

# --- Configuration (from environment) --------------------------------------

BASE_URL = os.environ.get("BOOKORBIT_URL", "http://localhost:3000").rstrip("/")
API_KEY = os.environ.get("BOOKORBIT_API_KEY", "").strip() or None
# Per-request timeout (seconds). Metadata writes regenerate covers server-side and
# can be slow, so default generously; override with BOOKORBIT_TIMEOUT.
TIMEOUT = float(os.environ.get("BOOKORBIT_TIMEOUT", "120"))
# Retries (with exponential backoff) for transient failures: timeouts and 429/5xx.
RETRIES = int(os.environ.get("BOOKORBIT_RETRIES", "2"))
BACKOFF = float(os.environ.get("BOOKORBIT_BACKOFF", "0.5"))

log = get_logger("bookorbit")

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


# --- BookOrbit HTTP client (API key) ----------------------------------------


class BookOrbitError(ToolError):
    """Raised when the BookOrbit API returns an error or auth is misconfigured.

    Subclasses FastMCP's ToolError so the message is treated as intentional,
    client-facing detail (and survives if mask_error_details is ever enabled).
    """


# HTTP statuses that warrant a transient retry (rate limit + gateway/unavailable).
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

# Actionable hints for BookOrbit's API-key error codes (docs/API_KEYS.md upstream).
_ERROR_HINTS = {
    "API_KEY_READ_ONLY": "This API key is read-only; use a 'Read and write' key for write tools.",
    "API_KEY_ROUTE_FORBIDDEN": "BookOrbit does not allow API keys on this route.",
    "API_KEYS_DISABLED": "API keys are turned off on this BookOrbit server "
    "(Settings > Admin > API keys).",
}


def _api_error(method: str, path: str, resp: httpx.Response) -> BookOrbitError:
    """Build a BookOrbitError from an error response, surfacing BookOrbit's
    `errorCode` and `message` (which names a missing permission on 403s)."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return BookOrbitError(f"{method} {path} -> {resp.status_code}: {resp.text}")

    code = body.get("errorCode")
    message = body.get("message")
    if isinstance(message, list):  # validation errors may arrive as a list
        message = "; ".join(str(m) for m in message)
    hint = _ERROR_HINTS.get(code) if isinstance(code, str) else None
    if hint is None and resp.status_code == 401:
        hint = "The API key was rejected: it is unknown, expired, revoked or malformed."
    parts = [f"{method} {path} -> {resp.status_code}"]
    if code:
        parts.append(f"[{code}]")
    message = str(message or resp.reason_phrase).rstrip(".")
    if hint and message.lower() in hint.lower():
        message = ""  # the hint already says it
    detail = f"{' '.join(parts)}: {message}" if message else " ".join(parts)
    if not hint:
        return BookOrbitError(detail)
    return BookOrbitError(f"{detail}. {hint}" if message else f"{detail}: {hint}")


# --- Book query building and validation (BookOrbit v3.8.0) -------------------
#
# BookOrbit validates `POST /books/query` bodies server-side but answers a bad one
# with a bare "Bad Request Exception", so queries are checked here first and fail
# with a message that says what is wrong. The tables mirror v3.8.0's
# packages/types/src/query.ts and server/src/modules/book/utils/group-rule.validator.ts.

# Max books per page; page * size may not exceed QUERY_OFFSET_MAX.
QUERY_PAGE_SIZE_MAX = 200
QUERY_OFFSET_MAX = 1_000_000
# Groups nest at most this deep, counting the top-level group.
FILTER_MAX_DEPTH = 5
SORT_SPECS_MAX = 5
RULE_LIST_VALUES_MAX = 20

_TEXT_OPS = ("contains", "notContains", "startsWith", "endsWith", "eq", "notEq")
_PRESENCE_OPS = ("isEmpty", "isNotEmpty")
_NUMERIC_OPS = ("eq", "notEq", "gt", "gte", "lt", "lte", "between")
_DATE_OPS = ("before", "after", "between", "withinLast", "moreThanAgo")
_SET_OPS = ("includesAny", "includesAll", "excludesAll")

# Rule field -> the operators BookOrbit accepts for it. List values for author,
# genre, tag, collection, library, format, readStatus and publisher/series/language
# set rules are matched by NAME (e.g. a collection's name, not its id).
FIELD_OPERATORS: dict[str, frozenset[str]] = {
    "title": frozenset(_TEXT_OPS + _PRESENCE_OPS),
    "publisher": frozenset(
        ("contains", "notContains", "eq", "notEq", "includesAny", "excludesAll") + _PRESENCE_OPS
    ),
    "language": frozenset(("eq", "notEq", "includesAny", "excludesAll") + _PRESENCE_OPS),
    "series": frozenset(
        ("contains", "notContains", "eq", "notEq", "includesAny", "excludesAll") + _PRESENCE_OPS
    ),
    "author": frozenset(_SET_OPS + _PRESENCE_OPS),
    "genre": frozenset(_SET_OPS + _PRESENCE_OPS),
    "tag": frozenset(_SET_OPS + _PRESENCE_OPS),
    "collection": frozenset(("includesAny", "excludesAll") + _PRESENCE_OPS),
    "library": frozenset(("includesAny", "excludesAll")),
    "format": frozenset(("includesAny", "excludesAll")),
    "fileSize": frozenset(_NUMERIC_OPS + _PRESENCE_OPS),
    "publishedDate": frozenset(_DATE_OPS + _PRESENCE_OPS),
    "publishedYear": frozenset(_NUMERIC_OPS + _PRESENCE_OPS),
    "seriesIndex": frozenset(_NUMERIC_OPS + _PRESENCE_OPS),
    "pageCount": frozenset(("gt", "gte", "lt", "lte", "between") + _PRESENCE_OPS),
    "addedAt": frozenset(_DATE_OPS),
    "startedAt": frozenset(_DATE_OPS + _PRESENCE_OPS),
    "finishedAt": frozenset(_DATE_OPS + _PRESENCE_OPS),
    "fileAvailability": frozenset(("isMissing", "isPresent")),
    "rating": frozenset(("eq", "gt", "gte", "lt", "lte") + _PRESENCE_OPS),
    "communityRating": frozenset(_NUMERIC_OPS + _PRESENCE_OPS),
    "communityRatingCount": frozenset(_NUMERIC_OPS + _PRESENCE_OPS),
    "readProgress": frozenset(("isUnread", "isInProgress", "isFinished")),
    "readStatus": frozenset(("includesAny", "excludesAll") + _PRESENCE_OPS),
    "description": frozenset(_PRESENCE_OPS),
    "isbn": frozenset(("eq",) + _PRESENCE_OPS),
    "metadataScore": frozenset(("gt", "gte", "lt", "lte", "between") + _PRESENCE_OPS),
    "cover": frozenset(("isMissing", "isPresent")),
    "audioCover": frozenset(("isMissing", "isPresent")),
    "lockStatus": frozenset(("isLocked", "isUnlocked")),
    "seriesStatus": frozenset(("isUpNext",)),
}
# Operators a custom metadata field (`custom:<id>`) may use, across all field types.
CUSTOM_FIELD_OPERATORS = frozenset(
    _TEXT_OPS + _PRESENCE_OPS + _NUMERIC_OPS + _DATE_OPS + ("isTrue", "isFalse")
)
_CUSTOM_FIELD_RE = re.compile(r"^custom:[1-9]\d{0,8}$")
_COMMUNITY_RATING_FIELDS = frozenset(("communityRating", "communityRatingCount"))
# Static rule fields whose before/after/between values must be dates.
_DATE_VALUE_FIELDS = frozenset(("addedAt", "startedAt", "finishedAt", "publishedDate"))
_DATE_KEY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)

SORT_FIELDS = frozenset(
    (
        "relevance",
        "author",
        "title",
        "series",
        "seriesIndex",
        "addedAt",
        "updatedAt",
        "publishedDate",
        "publishedYear",
        "pageCount",
        "rating",
        "publisher",
        "fileSize",
        "readProgress",
        "readStatus",
        "format",
        "lastReadAt",
        "startedAt",
        "finishedAt",
        "random",
        "language",
        "metadataScore",
        "collectionOrder",
    )
)
# Sort fields that only make sense inside one collection.
COLLECTION_SORT_FIELDS = frozenset(("collectionOrder",))

READ_STATUSES = (
    "unread",
    "want_to_read",
    "reading",
    "on_hold",
    "rereading",
    "read",
    "skimmed",
    "abandoned",
)


def rule(
    field: str,
    operator: str,
    value: Any = None,
    value_to: Any = None,
    *,
    provider: str | None = None,
) -> dict[str, Any]:
    """Build one filter rule, e.g. `rule("genre", "includesAny", ["Fantasy"])`."""
    node: dict[str, Any] = {"type": "rule", "field": field, "operator": operator}
    if value is not None:
        node["value"] = value
    if value_to is not None:
        node["valueTo"] = value_to
    if provider is not None:
        node["provider"] = provider
    return node


def all_of(*rules: dict[str, Any]) -> dict[str, Any]:
    """A group that matches books satisfying every rule (AND)."""
    return {"type": "group", "join": "AND", "rules": list(rules)}


def any_of(*rules: dict[str, Any]) -> dict[str, Any]:
    """A group that matches books satisfying at least one rule (OR)."""
    return {"type": "group", "join": "OR", "rules": list(rules)}


def sort_by(field: str, direction: Literal["asc", "desc"] = "asc") -> dict[str, str]:
    """One sort spec. BookOrbit's key is `dir`, not `direction`."""
    return {"field": field, "dir": direction}


def _is_rule_date(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(value)
    if not isinstance(value, str):
        return False
    v = value.strip()
    return bool(_DATE_KEY_RE.match(v) or _ISO_DATETIME_RE.match(v))


def _is_day_count(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, str) and value.strip():
        try:
            value = float(value)
        except ValueError:
            return False
    return isinstance(value, (int, float)) and value >= 0


def _validate_rule(node: dict[str, Any], where: str) -> None:
    unknown = set(node) - {"type", "field", "operator", "value", "valueTo", "provider"}
    if unknown:
        raise BookOrbitError(f"{where}: unknown rule keys {sorted(unknown)}.")
    field, op = node.get("field"), node.get("operator")
    if not isinstance(field, str):
        raise BookOrbitError(f"{where}: rule 'field' must be a string.")
    custom = bool(_CUSTOM_FIELD_RE.match(field))
    if not custom and field not in FIELD_OPERATORS:
        raise BookOrbitError(
            f"{where}: unknown filter field {field!r}. Valid fields: "
            f"{', '.join(sorted(FIELD_OPERATORS))}, or custom:<id>."
        )
    allowed = CUSTOM_FIELD_OPERATORS if custom else FIELD_OPERATORS[field]
    if op not in allowed:
        raise BookOrbitError(
            f"{where}: operator {op!r} is not valid for {field!r}. "
            f"Valid operators: {', '.join(sorted(allowed))}."
        )
    if "provider" in node and field not in _COMMUNITY_RATING_FIELDS:
        raise BookOrbitError(f"{where}: 'provider' only applies to community rating fields.")

    value, value_to = node.get("value"), node.get("valueTo")
    if isinstance(value, list):
        if custom:
            raise BookOrbitError(f"{where}: custom field rules do not accept list values.")
        if not 1 <= len(value) <= RULE_LIST_VALUES_MAX:
            raise BookOrbitError(
                f"{where}: list values need 1 to {RULE_LIST_VALUES_MAX} items, got {len(value)}."
            )
        all_str = all(isinstance(v, str) and v for v in value)
        all_num = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
        if not (all_str or all_num):
            raise BookOrbitError(
                f"{where}: list values must be all non-empty strings or all numbers."
            )
    elif value is not None and (
        isinstance(value, bool) or not isinstance(value, (str, int, float))
    ):
        raise BookOrbitError(f"{where}: value must be a string, number, or list.")
    if value_to is not None and (
        isinstance(value_to, bool) or not isinstance(value_to, (str, int, float))
    ):
        raise BookOrbitError(f"{where}: valueTo must be a string or number.")
    if op in _SET_OPS and not isinstance(value, list):
        raise BookOrbitError(f"{where}: operator {op!r} needs a list value, e.g. ['Fantasy'].")

    if field in _DATE_VALUE_FIELDS and op in ("before", "after", "between"):
        if not _is_rule_date(value):
            raise BookOrbitError(f"{where}: {op!r} needs a date value (YYYY-MM-DD).")
        if op == "between" and not _is_rule_date(value_to):
            raise BookOrbitError(f"{where}: 'between' needs a date valueTo (YYYY-MM-DD).")
    if field in _DATE_VALUE_FIELDS and op == "moreThanAgo" and not _is_day_count(value):
        raise BookOrbitError(f"{where}: 'moreThanAgo' needs a non-negative day count.")


def validate_filter(node: dict[str, Any], *, _depth: int = 1, _where: str = "filter") -> None:
    """Check a filter tree against BookOrbit's rules; raise BookOrbitError on the
    first problem, naming where in the tree it is."""
    if not isinstance(node, dict) or node.get("type") != "group":
        raise BookOrbitError(f"{_where}: expected a group ({{'type': 'group', ...}}).")
    if _depth > FILTER_MAX_DEPTH:
        raise BookOrbitError(f"{_where}: groups nest at most {FILTER_MAX_DEPTH} deep.")
    unknown = set(node) - {"type", "join", "rules"}
    if unknown:
        raise BookOrbitError(f"{_where}: unknown group keys {sorted(unknown)}.")
    if node.get("join") not in ("AND", "OR"):
        raise BookOrbitError(f"{_where}: join must be 'AND' or 'OR'.")
    rules = node.get("rules")
    if not isinstance(rules, list) or not rules:
        raise BookOrbitError(f"{_where}: a group needs at least one rule.")
    for i, child in enumerate(rules):
        where = f"{_where}.rules[{i}]"
        kind = child.get("type") if isinstance(child, dict) else None
        if kind == "group":
            validate_filter(child, _depth=_depth + 1, _where=where)
        elif kind == "rule":
            _validate_rule(child, where)
        else:
            raise BookOrbitError(f"{where}: expected a rule or a group (missing 'type').")


def validate_sort(sort: list[dict[str, str]], *, collection_scoped: bool = False) -> None:
    """Check sort specs against BookOrbit's sort fields."""
    if len(sort) > SORT_SPECS_MAX:
        raise BookOrbitError(f"At most {SORT_SPECS_MAX} sort specs are allowed.")
    for i, spec in enumerate(sort):
        if not isinstance(spec, dict) or set(spec) - {"field", "dir"}:
            raise BookOrbitError(f"sort[{i}]: expected {{'field': ..., 'dir': 'asc'|'desc'}}.")
        field = spec.get("field")
        if not (field in SORT_FIELDS or (isinstance(field, str) and _CUSTOM_FIELD_RE.match(field))):
            raise BookOrbitError(
                f"sort[{i}]: unknown sort field {field!r}. Valid fields: "
                f"{', '.join(sorted(SORT_FIELDS))}, or custom:<id>."
            )
        if field in COLLECTION_SORT_FIELDS and not collection_scoped:
            raise BookOrbitError(f"sort[{i}]: {field!r} only works inside one collection.")
        if spec.get("dir") not in ("asc", "desc"):
            raise BookOrbitError(f"sort[{i}]: dir must be 'asc' or 'desc'.")


def build_book_query(
    *,
    filter: dict[str, Any] | None = None,
    sort: list[dict[str, str]] | None = None,
    q: str | None = None,
    page: int = 0,
    size: int = 50,
    collection_scoped: bool = False,
) -> dict[str, Any]:
    """Build and validate a BookOrbit `BookQuery` body.

    Pages are 0-based, `size` is 1..200, and `page * size` may not exceed
    1,000,000. `q` is BookOrbit's free-text search (max 200 characters).
    """
    if not 1 <= size <= QUERY_PAGE_SIZE_MAX:
        raise BookOrbitError(f"Page size must be 1 to {QUERY_PAGE_SIZE_MAX}, got {size}.")
    if page < 0:
        raise BookOrbitError(f"Page must be 0 or more, got {page}.")
    if page * size > QUERY_OFFSET_MAX:
        raise BookOrbitError(f"Too deep: page * size must be at most {QUERY_OFFSET_MAX}.")
    query: dict[str, Any] = {"pagination": {"page": page, "size": size}}
    if filter is not None:
        validate_filter(filter)
        query["filter"] = filter
    if sort:
        validate_sort(sort, collection_scoped=collection_scoped)
        query["sort"] = sort
    if q:
        if len(q) > 200:
            raise BookOrbitError("Search text (q) may be at most 200 characters.")
        query["q"] = q
    return query


class BookOrbitClient:
    """Thin wrapper over the BookOrbit REST API, authenticated with an API key.

    The key is sent as `X-Api-Key` on every request. There is no session to log
    in to or refresh: a 401 means the key itself was rejected.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        timeout: float = 60.0,
        retries: int = 0,
        backoff: float = 0.5,
    ):
        self._base_url = base_url
        self._api_key = api_key
        self._timeout = timeout
        self._retries = max(0, retries)
        self._backoff = backoff
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    # -- transport (retry + wrap httpx errors as BookOrbitError) --------------

    async def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send a request, retrying transient failures (timeouts, connection
        errors, 429/5xx) with exponential backoff, and translating persistent
        httpx transport/timeout failures into a clean BookOrbitError."""
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
                    raise BookOrbitError(
                        f"{method} {path} timed out after {self._timeout}s. Raise "
                        f"BOOKORBIT_TIMEOUT if this server is slow on large operations."
                    ) from exc
                raise BookOrbitError(
                    f"Could not reach BookOrbit at {self._base_url} ({method} {path}): {exc}"
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

    def auth_headers(self) -> dict[str, str]:
        """Headers that authenticate a request, for calls that bypass `request()`
        (e.g. streaming). Raises if no API key is configured."""
        if not self._api_key:
            raise BookOrbitError(
                "BOOKORBIT_API_KEY must be set in the environment for the BookOrbit "
                "MCP server to authenticate. Create a key in BookOrbit under "
                "Settings > You > API keys."
            )
        return {"X-Api-Key": self._api_key}

    # -- requests --------------------------------------------------------------

    async def request(self, method: str, path: str, **kwargs: Any) -> Any:
        headers = {**kwargs.pop("headers", {}), **self.auth_headers()}
        resp = await self._send(method, path, headers=headers, **kwargs)

        if resp.status_code >= 400:
            log.warning("bookorbit api error", method=method, path=path, status=resp.status_code)
            raise _api_error(method, path, resp)

        log.debug("bookorbit request", method=method, path=path, status=resp.status_code)
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
        """Fetch the full book list (`GET /books`)."""
        return (
            await self.get(
                "/api/v1/books", params={"withDescription": str(with_description).lower()}
            )
            or []
        )

    # -- book queries ----------------------------------------------------------

    async def query_books(
        self,
        query: dict[str, Any],
        *,
        library_id: int | None = None,
        collection_id: int | None = None,
    ) -> dict[str, Any]:
        """Run one `BookQuery` and return the `BooksPage` (`items`, `total`, `page`,
        `size`). Scope it to one library or one collection, or neither (all books
        the key can see). Build `query` with `build_book_query`, which validates it:
        the server answers a malformed query with a bare 400."""
        if library_id is not None and collection_id is not None:
            raise BookOrbitError("Scope a query to a library or a collection, not both.")
        if library_id is not None:
            path = f"/api/v1/libraries/{library_id}/books"
        elif collection_id is not None:
            path = f"/api/v1/collections/{collection_id}/books/query"
        else:
            path = "/api/v1/books/query"
        return await self.post(path, json=query)

    async def iter_book_pages(
        self,
        *,
        max_books: int,
        filter: dict[str, Any] | None = None,
        sort: list[dict[str, str]] | None = None,
        q: str | None = None,
        library_id: int | None = None,
        collection_id: int | None = None,
        page_size: int = QUERY_PAGE_SIZE_MAX,
        ctx: Context | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Page through a query, yielding each `BooksPage` in order.

        Stops after `max_books` books (the last page is trimmed to fit) or when the
        results run out, so a caller never pulls an unbounded library into memory.
        A caller that needs to know whether it saw everything compares the books it
        received with `total` on any page. Reports progress to `ctx` per page.
        """
        if max_books < 1:
            raise BookOrbitError("max_books must be at least 1.")
        size = min(page_size, max_books)
        seen = 0
        page = 0
        while seen < max_books:
            query = build_book_query(
                filter=filter,
                sort=sort,
                q=q,
                page=page,
                size=size,
                collection_scoped=collection_id is not None,
            )
            result = await self.query_books(
                query, library_id=library_id, collection_id=collection_id
            )
            items = result.get("items") or []
            total = int(result.get("total") or 0)
            items = items[: max_books - seen]
            seen += len(items)
            if ctx is not None:
                await ctx.report_progress(progress=seen, total=min(total, max_books))
            yield {**result, "items": items}
            if not items or seen >= total or (page + 1) * size > QUERY_OFFSET_MAX:
                return
            page += 1

    async def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._http.aclose()


client = BookOrbitClient(
    BASE_URL,
    API_KEY,
    timeout=TIMEOUT,
    retries=RETRIES,
    backoff=BACKOFF,
)


# --- Helpers ----------------------------------------------------------------


class BookCardSummary(BaseModel):
    """Trimmed view of a BookOrbit `BookCard` (one item of a `BooksPage`)."""

    id: int
    title: str | None = None
    authors: list[str] = []
    series: str | None = None
    seriesIndex: str | None = None
    readStatus: str | None = None
    rating: float | None = None
    readingProgress: float | None = None
    genres: list[str] = []
    tags: list[str] = []
    formats: list[str] = []
    publishedYear: int | None = None
    libraryId: int | None = None


def _summarize_card(card: dict[str, Any]) -> BookCardSummary:
    """Trim a BookOrbit `BookCard` down to the fields most useful in a list view.

    A card's `readStatus` is an object (`{status, source, startedAt, ...}`) or null;
    the summary keeps just its `status`. `formats` lists each distinct file format,
    primary file first.
    """
    status = card.get("readStatus")
    formats: list[str] = []
    files = [f for f in card.get("files") or [] if isinstance(f, dict)]
    for f in sorted(files, key=lambda f: f.get("role") != "primary"):
        fmt = f.get("format")
        if fmt and fmt not in formats:
            formats.append(fmt)
    series_index = card.get("seriesIndex")
    return BookCardSummary(
        id=card["id"],
        title=card.get("title"),
        authors=[a for a in card.get("authors") or [] if isinstance(a, str)],
        series=card.get("seriesName"),
        seriesIndex=str(series_index) if series_index is not None else None,
        readStatus=status.get("status") if isinstance(status, dict) else None,
        rating=card.get("rating"),
        readingProgress=card.get("readingProgress"),
        genres=card.get("genres") or [],
        tags=card.get("tags") or [],
        formats=formats,
        publishedYear=card.get("publishedYear"),
        libraryId=card.get("libraryId"),
    )


class SearchResult(BaseModel):
    """Paginated result envelope for the book-listing tools."""

    total: int
    limit: int
    offset: int
    books: list[BookCardSummary]


async def _fetch_window(
    fetch: Callable[[int, int], Awaitable[dict[str, Any]]],
    *,
    offset: int,
    limit: int,
    max_page_size: int,
    items_key: str = "items",
    ctx: Context | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Fetch items [offset, offset + limit) from a 0-based paged endpoint.

    `fetch(page, size)` returns one page carrying `items_key` and `total`. Uses the
    fewest pages that cover the window (an offset that is not a multiple of the page
    size costs one extra page). Returns (items, total).
    """
    if limit < 1:
        raise BookOrbitError("limit must be at least 1.")
    if offset < 0:
        raise BookOrbitError("offset must be 0 or more.")
    size = min(max_page_size, limit)
    first, last = offset // size, (offset + limit - 1) // size
    items: list[dict[str, Any]] = []
    total = 0
    for page in range(first, last + 1):
        result = await fetch(page, size)
        batch = result.get(items_key) or []
        total = int(result.get("total") or 0)
        items.extend(batch)
        if ctx is not None:
            await ctx.report_progress(progress=page - first + 1, total=last - first + 1)
        if len(batch) < size:
            break
    start = offset - first * size
    return items[start : start + limit], total


async def _books_window(
    *,
    offset: int,
    limit: int,
    filter: dict[str, Any] | None = None,
    sort: list[dict[str, str]] | None = None,
    q: str | None = None,
    library_id: int | None = None,
    collection_id: int | None = None,
    ctx: Context | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """A window of BookCards from a (validated) book query; returns (cards, total)."""

    async def fetch(page: int, size: int) -> dict[str, Any]:
        query = build_book_query(
            filter=filter,
            sort=sort,
            q=q,
            page=page,
            size=size,
            collection_scoped=collection_id is not None,
        )
        return await client.query_books(query, library_id=library_id, collection_id=collection_id)

    return await _fetch_window(
        fetch, offset=offset, limit=limit, max_page_size=QUERY_PAGE_SIZE_MAX, ctx=ctx
    )


async def _names_by_id(path: str, ids: list[int], what: str, lister: str) -> list[str]:
    """Map library or collection ids to names. BookOrbit's `library` and `collection`
    filter rules match by name, and an id silently matches nothing."""
    rows = await client.get(path) or []
    names: dict[int, str] = {
        r["id"]: str(r["name"]) for r in rows if isinstance(r, dict) and r.get("name")
    }
    unknown = [i for i in ids if i not in names]
    if unknown:
        raise BookOrbitError(f"Unknown {what} id(s): {unknown}. Use {lister} to find ids.")
    return [names[i] for i in ids]


# search_books `missing` field -> the BookOrbit rule that is true when it is empty.
_MISSING_RULES: dict[str, dict[str, Any]] = {
    "tags": rule("tag", "isEmpty"),
    "genres": rule("genre", "isEmpty"),
    "authors": rule("author", "isEmpty"),
    "collections": rule("collection", "isEmpty"),
    "series": rule("series", "isEmpty"),
    "description": rule("description", "isEmpty"),
    "isbn": rule("isbn", "isEmpty"),
    "publisher": rule("publisher", "isEmpty"),
    "language": rule("language", "isEmpty"),
    "pageCount": rule("pageCount", "isEmpty"),
    "publishedDate": rule("publishedDate", "isEmpty"),
    "cover": rule("cover", "isMissing"),
    "rating": rule("rating", "isEmpty"),
    "communityRating": rule("communityRating", "isEmpty", provider="any"),
    "amazonRating": rule("communityRating", "isEmpty", provider="amazon"),
    "goodreadsRating": rule("communityRating", "isEmpty", provider="goodreads"),
    "googleRating": rule("communityRating", "isEmpty", provider="google"),
    "hardcoverRating": rule("communityRating", "isEmpty", provider="hardcover"),
}

_SEARCH_FILTER_KEYS = (
    "tags",
    "tags_mode",
    "genres",
    "genres_mode",
    "authors",
    "collection_ids",
    "library_ids",
    "read_status",
    "missing",
    "missing_mode",
    "rules",
)


def _mode(filters: dict[str, Any], key: str) -> str:
    mode = filters.get(key, "any")
    if mode not in ("any", "all"):
        raise BookOrbitError(f"{key} must be 'any' or 'all', got {mode!r}.")
    return str(mode)


async def _search_filter(filters: dict[str, Any]) -> dict[str, Any] | None:
    """Translate search_books `filters` into one BookOrbit filter group (or None)."""
    unknown = sorted(set(filters) - set(_SEARCH_FILTER_KEYS))
    if unknown:
        raise BookOrbitError(
            f"Unknown filter key(s): {', '.join(unknown)}. Valid: {', '.join(_SEARCH_FILTER_KEYS)}."
        )
    rules: list[dict[str, Any]] = []
    if tags := filters.get("tags"):
        op = "includesAll" if _mode(filters, "tags_mode") == "all" else "includesAny"
        rules.append(rule("tag", op, list(tags)))
    if genres := filters.get("genres"):
        op = "includesAll" if _mode(filters, "genres_mode") == "all" else "includesAny"
        rules.append(rule("genre", op, list(genres)))
    if authors := filters.get("authors"):
        rules.append(rule("author", "includesAny", list(authors)))
    if collection_ids := filters.get("collection_ids"):
        names = await _names_by_id(
            "/api/v1/collections", list(collection_ids), "collection", "list_collections"
        )
        rules.append(rule("collection", "includesAny", names))
    if library_ids := filters.get("library_ids"):
        names = await _names_by_id(
            "/api/v1/libraries", list(library_ids), "library", "list_libraries"
        )
        rules.append(rule("library", "includesAny", names))
    if status := filters.get("read_status"):
        statuses = [status] if isinstance(status, str) else list(status)
        bad = [s for s in statuses if s not in READ_STATUSES]
        if bad:
            raise BookOrbitError(
                f"Unknown read_status {bad}. Valid: {', '.join(READ_STATUSES)} "
                f"('unread' also matches books with no status)."
            )
        rules.append(rule("readStatus", "includesAny", statuses))
    if missing := filters.get("missing"):
        bad = sorted(set(missing) - set(_MISSING_RULES))
        if bad:
            raise BookOrbitError(
                f"Unknown missing field(s): {', '.join(bad)}. Valid: {', '.join(_MISSING_RULES)}."
            )
        empties = [dict(_MISSING_RULES[f]) for f in missing]
        if filters.get("missing_mode", "all") not in ("any", "all"):
            raise BookOrbitError("missing_mode must be 'any' or 'all'.")
        if len(empties) == 1:
            rules.extend(empties)
        else:
            rules.append(
                any_of(*empties) if filters.get("missing_mode") == "any" else all_of(*empties)
            )
    if raw := filters.get("rules"):
        rules.append(raw)
    if not rules:
        return None
    combined = all_of(*rules)
    validate_filter(combined)
    return combined


def _sort_spec(sort: str, order: str) -> list[dict[str, str]]:
    if order not in ("asc", "desc"):
        raise BookOrbitError("order must be 'asc' or 'desc'.")
    return [sort_by(sort, order)]  # type: ignore[arg-type]


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


def _meta(book: dict) -> dict:
    return book.get("metadata") or {}


def _card_status(card: dict) -> str | None:
    status = card.get("readStatus")
    return status.get("status") if isinstance(status, dict) else None


# export_library columns, read from a BookOrbit `BookCard`.
_EXPORT_FIELDS: dict[str, Any] = {
    "id": lambda c: c.get("id"),
    "title": lambda c: c.get("title"),
    "subtitle": lambda c: c.get("subtitle"),
    "authors": lambda c: list(c.get("authors") or []),
    "series": lambda c: c.get("seriesName"),
    "seriesIndex": lambda c: c.get("seriesIndex"),
    "publisher": lambda c: c.get("publisher"),
    "publishedDate": lambda c: c.get("publishedDate"),
    "publishedYear": lambda c: c.get("publishedYear"),
    "language": lambda c: c.get("language"),
    "pageCount": lambda c: c.get("pageCount"),
    "isbn13": lambda c: c.get("isbn13"),
    "hardcoverId": lambda c: c.get("hardcoverId"),
    "genres": lambda c: list(c.get("genres") or []),
    "tags": lambda c: list(c.get("tags") or []),
    "narrators": lambda c: list(c.get("narrators") or []),
    "rating": lambda c: c.get("rating"),
    "readStatus": _card_status,
    "readingProgress": lambda c: c.get("readingProgress"),
    "formats": lambda c: _summarize_card(c).formats,
    "addedAt": lambda c: c.get("addedAt"),
    "metadataScore": lambda c: c.get("metadataScore"),
    "lockedFields": lambda c: list(c.get("lockedFields") or []),
    "libraryId": lambda c: c.get("libraryId"),
}
_EXPORT_DEFAULT_FIELDS = [
    "id",
    "title",
    "authors",
    "series",
    "readStatus",
    "rating",
    "genres",
    "tags",
]
EXPORT_LIMIT_MAX = 5000


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
        raise BookOrbitError(
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
    name="bookorbit",
    lifespan=lifespan,
    instructions=(
        "Tools for a self-hosted BookOrbit library. Book, collection and library IDs "
        "are not guessable: use list_books / search_books, list_collections and "
        "list_libraries to discover them before calling tools that take an ID. The "
        "listing tools return trimmed summaries; call get_book for the full record, "
        "including the file ids that reading-progress tools need."
    ),
)


# ---- Read tools ------------------------------------------------------------


@mcp.tool(
    annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List / search books"}
)
async def list_books(query: str = "", limit: int = 25) -> list[BookCardSummary]:
    """List books, optionally matching a free-text `query` (BookOrbit's own search over
    title, authors, series, ISBN and more). Returns up to `limit` (max 200) trimmed
    summaries sorted by title. For filters, sorting or paging use search_books; for
    one book's full record use get_book."""
    cards, _ = await _books_window(
        offset=0,
        limit=max(1, min(limit, QUERY_PAGE_SIZE_MAX)),
        q=query or None,
        sort=[sort_by("relevance", "desc")] if query else [sort_by("title")],
    )
    return [_summarize_card(c) for c in cards]


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Get book details"})
async def get_book(book_id: int, with_description: bool = True) -> dict:
    """Fetch one book's full record by ID: metadata, authors, genres, tags, series,
    files (with file ids), collections, provider ids/links, community ratings, locked
    fields, your read status, rating and note. Set with_description=False to drop the
    (often long) description. Find ids with list_books / search_books."""
    book = await client.get(f"/api/v1/books/{book_id}")
    if not with_description and isinstance(book, dict):
        book.pop("description", None)
    return book


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List collections"})
async def list_collections() -> list[dict]:
    """List the collections you can see (yours and public ones): id, name,
    description, book count, and whether it's public or yours. Use
    get_collection_books to see the books in one."""
    rows = await client.get("/api/v1/collections") or []
    return [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "description": c.get("description"),
            "book_count": c.get("bookCount"),
            "is_public": c.get("isPublic"),
            "is_owner": c.get("isOwner"),
        }
        for c in rows
        if isinstance(c, dict)
    ]


@mcp.tool(
    annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Get books in a collection"}
)
async def get_collection_books(
    collection_id: int,
    sort: str = "collectionOrder",
    order: Literal["asc", "desc"] = "asc",
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    """List the books in a collection as trimmed summaries, paginated. Sorted by the
    collection's own order by default; any search_books sort field also works. Get
    collection ids from list_collections."""
    cards, total = await _books_window(
        offset=offset,
        limit=max(1, min(limit, QUERY_PAGE_SIZE_MAX)),
        sort=_sort_spec(sort, order),
        collection_id=collection_id,
    )
    return SearchResult(
        total=total, limit=limit, offset=offset, books=[_summarize_card(c) for c in cards]
    )


_RELATED_PATHS = {
    "similar": "recommendations",
    "series": "series-books",
    "author": "author-books",
}


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Get related books"})
async def get_recommendations(
    book_id: int,
    kind: Literal["similar", "series", "author"] = "similar",
    limit: int = 10,
) -> list[dict]:
    """Books related to the given one: `similar` (BookOrbit's recommendations, up to
    25), other books in the same `series`, or more by the same `author`. Returns
    trimmed entries (id, title, authors, read status)."""
    rows = await client.get(f"/api/v1/books/{book_id}/{_RELATED_PATHS[kind]}") or []
    out = []
    for b in rows[: max(1, limit)]:
        if not isinstance(b, dict):
            continue
        entry = {
            "id": b.get("id"),
            "title": b.get("title"),
            "authors": b.get("authors") or [],
            "readStatus": _card_status(b),
        }
        if b.get("seriesIndex") is not None:
            entry["seriesIndex"] = b.get("seriesIndex")
        out.append(entry)
    return out


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
    sort: str = "title",
    order: Literal["asc", "desc"] = "asc",
    limit: int = 50,
    offset: int = 0,
) -> SearchResult:
    """Search and filter the library on the server, returning one page of summaries.

    `query` is BookOrbit's free-text search (title, authors, series, ISBN, ...).
    `filters` is an optional object; every key narrows the result (AND):
      - tags: [str], tags_mode: "any" (default) | "all" — exact tag names
      - genres: [str], genres_mode: "any" | "all" — exact genre names
      - authors: [str] — any author whose name contains one of these
      - collection_ids: [int], library_ids: [int] — from list_collections /
        list_libraries
      - read_status: str or [str] — unread, want_to_read, reading, on_hold, rereading,
        read, skimmed, abandoned ("unread" also matches books never marked)
      - missing: fields that must be empty — tags, genres, authors, collections,
        series, description, isbn, publisher, language, pageCount, publishedDate,
        cover, rating (yours), communityRating (from any provider), amazonRating,
        goodreadsRating, googleRating, hardcoverRating
      - missing_mode: "all" (default) keeps books where every listed field is empty;
        "any" keeps books missing at least one (good for enrichment sweeps)
      - rules: a raw BookOrbit filter group, for anything the keys above don't cover
    Lists take at most 20 values. `sort` is one of: title, author, series, seriesIndex,
    addedAt, updatedAt, publishedDate, publishedYear, pageCount, rating, publisher,
    fileSize, readProgress, readStatus, format, lastReadAt, startedAt, finishedAt,
    language, metadataScore, relevance (with `query`), random. `limit` is capped at 200.
    Returns {total, limit, offset, books}.
    """
    flt = await _search_filter(filters or {})
    limit = max(1, min(limit, QUERY_PAGE_SIZE_MAX))
    cards, total = await _books_window(
        offset=offset, limit=limit, filter=flt, sort=_sort_spec(sort, order), q=query or None
    )
    return SearchResult(
        total=total, limit=limit, offset=offset, books=[_summarize_card(c) for c in cards]
    )


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List authors"})
async def list_authors(query: str = "", limit: int = 100, offset: int = 0) -> dict:
    """List authors with per-author book counts, sorted by name. `query` filters by
    name (BookOrbit's author search). Returns {total, authors:[{id, name, book_count}]}."""

    async def fetch(page: int, size: int) -> dict[str, Any]:
        params: dict[str, Any] = {"page": page, "size": size}
        if query:
            params["q"] = query
        return await client.get("/api/v1/authors", params=params)

    rows, total = await _fetch_window(
        fetch, offset=offset, limit=max(1, min(limit, 500)), max_page_size=100
    )
    return {
        "total": total,
        "authors": [
            {"id": a.get("id"), "name": a.get("name"), "book_count": a.get("bookCount")}
            for a in rows
        ],
    }


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Books by author"})
async def books_by_author(author: str, limit: int = 50, offset: int = 0) -> SearchResult:
    """List books by an author. Matches any author whose name contains `author`
    (case- and accent-insensitive), so "tolkien" finds "J.R.R. Tolkien". Sorted by
    publication year, then title."""
    limit = max(1, min(limit, QUERY_PAGE_SIZE_MAX))
    cards, total = await _books_window(
        offset=offset,
        limit=limit,
        filter=all_of(rule("author", "includesAny", [author])),
        sort=[sort_by("publishedYear"), sort_by("title")],
    )
    return SearchResult(
        total=total, limit=limit, offset=offset, books=[_summarize_card(c) for c in cards]
    )


async def _count_books(flt: dict[str, Any] | None = None) -> int:
    page = await client.query_books(build_book_query(filter=flt, size=1))
    return int(page.get("total") or 0)


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Reading stats"})
async def get_reading_stats() -> dict:
    """Your reading stats: book counts by read status (they add up to the total;
    books never marked count as unread), your ratings (count, average and 1-5
    distribution), and BookOrbit's reading summary (tracked, in progress, completed,
    mean progress). Counts come from small server-side queries, not a library scan."""
    status_flts = [all_of(rule("readStatus", "includesAny", [s])) for s in READ_STATUSES]
    rating_flts = [all_of(rule("rating", "eq", n)) for n in range(1, 6)]
    total, *counts = await asyncio.gather(
        _count_books(), *(_count_books(f) for f in status_flts + rating_flts)
    )
    by_status = dict(zip(READ_STATUSES, counts[: len(READ_STATUSES)], strict=True))
    ratings = dict(zip(range(1, 6), counts[len(READ_STATUSES) :], strict=True))
    rated = sum(ratings.values())
    avg = round(sum(n * c for n, c in ratings.items()) / rated, 2) if rated else None
    result: dict[str, Any] = {
        "total": total,
        "by_status": by_status,
        "rated_count": rated,
        "avg_rating": avg,
        "ratings": ratings,
    }
    with suppress(BookOrbitError):
        result["reading"] = await client.get("/api/v1/user-statistics/summary")
    return result


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Export library"})
async def export_library(
    format: Literal["json", "csv"] = "json",
    query: str = "",
    filters: dict | None = None,
    collection_id: int | None = None,
    library_id: int | None = None,
    fields: list[str] | None = None,
    full: bool = False,
    offset: int = 0,
    limit: int = 500,
    ctx: Context | None = None,
) -> dict:
    """Export books as JSON or CSV: the whole library, one collection or library, or
    any search_books `query`/`filters` selection, sorted by title.

    Default columns: id, title, authors, series, readStatus, rating, genres, tags.
    Pass `fields` to choose columns, or `full=True` for all of them: id, title,
    subtitle, authors, series, seriesIndex, publisher, publishedDate, publishedYear,
    language, pageCount, isbn13, hardcoverId, genres, tags, narrators, rating,
    readStatus, readingProgress, formats, addedAt, metadataScore, lockedFields,
    libraryId. (Descriptions and per-provider ids are only on get_book.)

    Large exports can exceed the response size limit, so prefer `csv`, keep `fields`
    narrow, and page with `offset`/`limit` (max 5000 per call); `total` says how many
    books match. Returns {format, total, offset, limit, returned, fields, content}.
    """
    if fields:
        bad = sorted(set(fields) - set(_EXPORT_FIELDS))
        if bad:
            raise BookOrbitError(
                f"Unknown export field(s): {', '.join(bad)}. "
                f"Available: {', '.join(_EXPORT_FIELDS)}."
            )
        cols = list(fields)
    elif full:
        cols = list(_EXPORT_FIELDS)
    else:
        cols = list(_EXPORT_DEFAULT_FIELDS)
    limit = max(1, min(limit, EXPORT_LIMIT_MAX))

    cards, total = await _books_window(
        offset=offset,
        limit=limit,
        filter=await _search_filter(filters or {}),
        q=query or None,
        sort=[sort_by("title")],
        library_id=library_id,
        collection_id=collection_id,
        ctx=ctx,
    )
    rows = [{c: _EXPORT_FIELDS[c](card) for c in cols} for card in cards]

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
    authenticated=false means it is up but the API key is missing or was rejected, so
    the client can tell "down" from "bad key" before a long batch.

    Once authenticated, also reports the server version, the key's owner and its
    effective permissions (a key can hold fewer than its owner), and library and
    book counts.
    """
    result: dict[str, Any] = {"ok": False, "authenticated": False}

    # Public liveness probe: no credential needed.
    try:
        health = (await client._http.get("/api/v1/health")).json()
    except Exception as exc:  # transport-level failure or non-JSON reply
        result["error"] = str(exc)
        return result
    result["ok"] = isinstance(health, dict) and health.get("status") == "ok"
    if not result["ok"]:
        result["error"] = f"health check reported: {health}"
        return result

    try:
        me = await client.get("/api/v1/auth/me")
    except BookOrbitError as exc:
        result["error"] = str(exc)
        return result
    result.update(
        authenticated=True,
        user=me.get("username"),
        authentication_method=me.get("authenticationMethod"),
        permissions=me.get("permissions") or [],
    )
    with suppress(BookOrbitError):
        info = await client.get("/api/v1/app-info")
        result["server_version"] = info.get("version")
        if info.get("updateAvailable"):
            result["latest_version"] = info.get("latestVersion")
    with suppress(BookOrbitError):
        result["library_count"] = len(await client.get("/api/v1/libraries") or [])
    with suppress(BookOrbitError):
        result["book_count"] = (await client.get("/api/v1/statistics/summary"))["totalBooks"]
    return result


# ---- Bulk & additive writes ------------------------------------------------


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
        raise BookOrbitError("Pass either book_ids+patch OR items, not both.")
    if not items and not (book_ids and patch):
        raise BookOrbitError("Provide book_ids+patch, or items.")

    work = items if items else [{"book_id": bid, "patch": patch} for bid in (book_ids or [])]
    total = len(work)

    updated: list[int] = []
    failed: list[dict] = []
    books: list[dict] = []
    for done, entry in enumerate(work, start=1):
        bid = entry.get("book_id")
        try:
            if not isinstance(bid, int):
                raise BookOrbitError("each item needs an integer 'book_id'")
            books.append(await _apply_patch(bid, entry.get("patch") or {}))
            updated.append(bid)
        except Exception as exc:
            failed.append({"book_id": bid, "error": str(exc)})
        if ctx is not None:
            await ctx.report_progress(progress=done, total=total)
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
        raise BookOrbitError("Provide at least one field in `lock` or `unlock`.")
    return await client.put(
        "/api/v1/books/metadata/toggle-field-locks",
        json={"bookIds": book_ids, "fieldActions": field_actions},
    )


# ---- Metadata lookups ------------------------------------------------------

_METADATA_STREAM_PATH = "/api/v1/metadata-fetch/stream"
# External providers can be slow; the stream ends when every provider has answered.
METADATA_STREAM_TIMEOUT = 180.0
# Named SSE event BookOrbit uses for a provider that timed out, was throttled or failed.
_PROVIDER_STATUS_EVENT = "provider-status"


async def _stream_metadata_search(params: dict[str, Any]) -> tuple[list[dict], dict[str, str]]:
    """Read BookOrbit's metadata search stream (server-sent events) to the end.

    Unnamed events are candidates, one per provider match. `provider-status` events
    name a provider whose search timed out, was throttled or failed. Returns
    (candidates, {provider: outcome}).
    """
    candidates: list[dict] = []
    failures: dict[str, str] = {}
    event: str | None = None
    data: list[str] = []

    def dispatch() -> None:
        if not data:
            return
        with suppress(json.JSONDecodeError):
            payload = json.loads("\n".join(data))
            if not isinstance(payload, dict):
                return
            if event == _PROVIDER_STATUS_EVENT:
                if payload.get("provider"):
                    failures[str(payload["provider"])] = str(payload.get("outcome") or "failed")
            else:
                candidates.append(payload)

    headers = {**client.auth_headers(), "Accept": "text/event-stream"}
    try:
        async with client._http.stream(
            "GET",
            _METADATA_STREAM_PATH,
            params=params,
            headers=headers,
            timeout=METADATA_STREAM_TIMEOUT,
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                raise _api_error("GET", _METADATA_STREAM_PATH, resp)
            async for line in resp.aiter_lines():
                if not line:
                    dispatch()
                    event, data = None, []
                    continue
                if line.startswith(":"):
                    continue
                name, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if name == "event":
                    event = value
                elif name == "data":
                    data.append(value)
            dispatch()
    except httpx.TimeoutException as exc:
        raise BookOrbitError(
            "The metadata search timed out: external providers can be slow. "
            "Try fewer providers or retry."
        ) from exc
    except httpx.TransportError as exc:
        raise BookOrbitError(f"Could not reach BookOrbit at {client._base_url}: {exc}") from exc
    return candidates, failures


async def _search_metadata(params: dict[str, Any], providers: list[str] | None) -> dict:
    """Search the enabled metadata providers and report each one's outcome."""
    rows = await client.get("/api/v1/metadata-fetch/providers") or []
    enabled = [str(r["key"]) for r in rows if isinstance(r, dict) and r.get("key")]
    requested = list(dict.fromkeys(providers)) if providers else enabled
    active = [p for p in requested if p in enabled]

    candidates: list[dict] = []
    failures: dict[str, str] = {}
    if active:
        candidates, failures = await _stream_metadata_search(
            {**params, "providers": ",".join(active)}
        )

    counts = Counter(str(c.get("provider")) for c in candidates)
    statuses: list[dict[str, Any]] = []
    for p in requested:
        if p not in enabled:
            statuses.append(
                {
                    "provider": p,
                    "status": "disabled",
                    "count": 0,
                    "reason": "not enabled on this server. "
                    f"Enabled: {', '.join(enabled) or 'none'}.",
                }
            )
        elif p in failures:
            statuses.append({"provider": p, "status": failures[p], "count": counts[p]})
        elif counts[p]:
            statuses.append({"provider": p, "status": "ok", "count": counts[p]})
        else:
            statuses.append(
                {"provider": p, "status": "empty", "count": 0, "reason": "ran and found no match"}
            )
    return {
        "providers": requested,
        "provider_status": statuses,
        "count": len(candidates),
        "candidates": candidates,
    }


@mcp.tool(
    annotations={
        "readOnlyHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
        "title": "Look up metadata by ISBN",
    }
)
async def isbn_lookup(isbn: str, providers: list[str] | None = None) -> dict:
    """Look up an ISBN with BookOrbit's metadata providers (Google Books, Goodreads,
    Amazon, OpenLibrary, ... whichever are enabled on the server). Read-only: changes
    no book. `providers` limits the search to some provider keys (lowercase, e.g.
    ["google", "openLibrary"]); by default every enabled provider is asked.
    Returns {isbn, providers, provider_status, count, candidates} — see
    fetch_metadata_candidates for what provider_status means."""
    return {"isbn": isbn, **await _search_metadata({"isbn": isbn}, providers)}


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
    """Fetch candidate metadata for a book from external providers. Read-only: it
    does NOT change the book.

    With just `book_id`, BookOrbit searches using the book's own stored metadata and
    provider ids. Pass `isbn`, `title` or `author` to search with other terms.
    `providers` limits the search to some provider keys (lowercase, e.g. ["google",
    "goodreads", "amazon"]); by default every enabled provider is asked. Each
    candidate carries its `provider`, `providerId` and that provider's fields.

    `provider_status` reports each requested provider as: ok (n candidates), empty
    (ran but found no match), timeout / throttled / failed (the provider's search
    broke, so its silence is not a real no-match), or disabled (not enabled on this
    server, or not a provider key). Returns {book_id, providers, provider_status,
    count, candidates}.
    """
    params: dict[str, Any] = {"bookId": book_id}
    for key, value in (("isbn", isbn), ("title", title), ("author", author)):
        if value:
            params[key] = value
    return {"book_id": book_id, **await _search_metadata(params, providers)}


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
async def bulk_set_read_status(items: list[dict], ctx: Context | None = None) -> dict:
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
    # One backend call per status group; report books processed as each lands.
    total = sum(len(ids) for ids in groups.values())
    for status, ids in groups.items():
        await client.post("/api/v1/books/status", json={"bookIds": ids, "status": status})
        updated.extend(ids)
        by_status[status] = len(ids)
        if ctx is not None:
            await ctx.report_progress(progress=len(updated), total=total)
    return {"updated": updated, "by_status": by_status, "failed": failed}


# Duplicate scans (BookOrbit v3.8.0 book-duplicates module).
_DUP_REASONS = {
    "duplicate": ("file_hash", "isbn", "exact_metadata", "fuzzy_metadata"),
    "other_format": ("same_work", "title_author", "series_position"),
}
_DUP_GROUPS_PAGE_SIZE = 50
# Seconds between scan status checks while waiting for a scan to finish.
DUP_POLL_INTERVAL = 2.0


def _dup_scan_summary(scan: dict) -> dict:
    return {
        "scan_id": scan.get("id"),
        "status": scan.get("status"),
        "library_ids": scan.get("libraryIds"),
        "similarity_percent": scan.get("similarityPercent"),
        "progress_percent": scan.get("progressPercent"),
        "total_groups": scan.get("totalGroups"),
        "total_other_format_pairs": scan.get("totalOtherFormatPairs"),
        "total_reclaimable_bytes": scan.get("totalReclaimableBytes"),
    }


def _dup_group(group: dict) -> dict:
    books = [b for b in group.get("books") or [] if isinstance(b, dict)]
    isbns = {b.get("isbn13") or b.get("isbn10") for b in books} - {None}
    return {
        "id": group.get("id"),
        "kind": group.get("kind"),
        "reasons": group.get("reasons") or [],
        "max_title_similarity": group.get("maxTitleSimilarity"),
        "reclaimable_bytes": group.get("reclaimableBytes"),
        "distinct_isbns": len(isbns) > 1,
        "books": [
            {
                "id": b.get("id"),
                "title": b.get("title"),
                "authors": b.get("authors") or [],
                "isbn13": b.get("isbn13"),
                "isbn10": b.get("isbn10"),
                "formats": sorted(
                    {f.get("format") for f in b.get("files") or [] if f.get("format")}
                ),
                "library": b.get("libraryName"),
                "metadata_score": b.get("metadataScore"),
            }
            for b in books
        ],
    }


@mcp.tool(annotations={"destructiveHint": False, "title": "Find duplicates"})
async def find_duplicates(
    library_id: int | None = None,
    similarity_percent: int = 90,
    kind: Literal["duplicate", "other_format"] = "duplicate",
    reason: str | None = None,
    max_groups: int = 50,
    scan_id: int | None = None,
    wait_seconds: float = 120,
    ctx: Context | None = None,
) -> dict:
    """Find duplicate books with BookOrbit's duplicate scanner. It never merges or
    deletes anything: it only reports groups.

    Runs (or reuses) a scan over one library or, by default, every library you can
    access, then lists its groups. `kind` "duplicate" finds copies of the same book
    (reasons: file_hash, isbn, exact_metadata, fuzzy_metadata); "other_format" finds
    the same work in another format (same_work, title_author, series_position).
    `reason` narrows to one reason. `similarity_percent` (70-100) sets how close
    fuzzy title matches must be. Each group lists its books and `distinct_isbns`
    (true means the books carry different ISBNs: likely separate editions).

    A scan runs in the background. If it is still running after `wait_seconds`, this
    returns status "running" and its scan_id: call again with that `scan_id` to
    collect the result. Needs an API key with read-and-write access and the
    library_delete_books permission (BookOrbit gates the scanner behind it).
    """
    if not 70 <= similarity_percent <= 100:
        raise BookOrbitError("similarity_percent must be between 70 and 100.")
    if reason is not None and reason not in _DUP_REASONS[kind]:
        raise BookOrbitError(
            f"reason {reason!r} does not apply to kind {kind!r}. "
            f"Valid: {', '.join(_DUP_REASONS[kind])}."
        )
    max_groups = max(1, max_groups)

    reused = False
    if scan_id is not None:
        scan = await client.get(f"/api/v1/book-duplicates/scans/{scan_id}")
    else:
        scan = await client.get("/api/v1/book-duplicates/scans/active")
        reused = bool(scan)
        if not scan:
            body: dict[str, Any] = {"similarityPercent": similarity_percent}
            if library_id is not None:
                body["libraryId"] = library_id
            scan = await client.post("/api/v1/book-duplicates/scans", json=body)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0.0, wait_seconds)
    while scan.get("status") in ("queued", "running"):
        if ctx is not None and scan.get("progressPercent") is not None:
            await ctx.report_progress(progress=scan["progressPercent"], total=100)
        if loop.time() >= deadline:
            return {
                **_dup_scan_summary(scan),
                "reused_active_scan": reused,
                "message": f"Scan still running: call find_duplicates(scan_id={scan.get('id')}) "
                "again to collect the groups.",
            }
        await asyncio.sleep(DUP_POLL_INTERVAL)
        scan = await client.get(f"/api/v1/book-duplicates/scans/{scan['id']}")

    if scan.get("status") != "completed":
        raise BookOrbitError(
            f"Duplicate scan {scan.get('id')} {scan.get('status')}"
            + (f" ({scan['errorCode']})" if scan.get("errorCode") else "")
            + "."
        )

    groups: list[dict] = []
    total = 0
    page = 1  # this endpoint's pages start at 1
    while len(groups) < max_groups:
        params: dict[str, Any] = {"page": page, "pageSize": _DUP_GROUPS_PAGE_SIZE, "kind": kind}
        if reason:
            params["reason"] = reason
        result = await client.get(
            f"/api/v1/book-duplicates/scans/{scan['id']}/groups", params=params
        )
        batch = result.get("groups") or []
        total = int(result.get("total") or 0)
        groups.extend(_dup_group(g) for g in batch)
        if len(batch) < _DUP_GROUPS_PAGE_SIZE or len(groups) >= total:
            break
        page += 1
    groups = groups[:max_groups]
    return {
        **_dup_scan_summary(scan),
        "reused_active_scan": reused,
        "kind": kind,
        "group_count": total,
        "returned": len(groups),
        "groups": groups,
    }


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "List libraries"})
async def list_libraries() -> list[dict]:
    """List your BookOrbit libraries: id, name, book count, whether its folders are
    watched for new files, its folders on the server (id + path), and allowed
    formats. Use it to see where a manually added file lands and which library to
    rescan, or to get ids for search_books' library_ids filter."""
    libs = await client.get("/api/v1/libraries") or []
    return [
        {
            "id": lib.get("id"),
            "name": lib.get("name"),
            "book_count": lib.get("bookCount"),
            "watch": lib.get("watch"),
            "paths": [
                {"id": f.get("id"), "path": f.get("path")}
                for f in (lib.get("folders") or [])
                if isinstance(f, dict)
            ],
            "allowed_formats": lib.get("allowedFormats"),
        }
        for lib in libs
        if isinstance(lib, dict)
    ]


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True, "title": "Refresh library"})
async def refresh_library(library_id: int) -> dict:
    """Trigger a rescan of a library's paths so BookLore ingests files added on disk.

    Use after manually placing a book (e.g. a safaribooks EPUB) into one of the
    library's paths — BookLore re-walks the paths, imports new files and reads their
    embedded metadata. Scanning runs server-side and may take a moment to reflect in
    the book list. Use `list_libraries` to find the id. Returns {library_id, status}.
    """
    await client.put(f"/api/v1/libraries/{library_id}/refresh")
    return {"library_id": library_id, "status": "rescan triggered"}


@mcp.tool(annotations={"idempotentHint": True, "openWorldHint": True, "title": "Rescan bookdrop"})
async def bookdrop_rescan() -> dict:
    """Trigger a rescan of BookLore's bookdrop folder for files added on disk.

    BookLore watches a dedicated "bookdrop" folder; files dropped there are staged for
    review before import. Use this to pick up files added while the watcher was down or
    that it missed. Staged files still need finalizing in the BookLore UI before they
    enter a library. Returns {status}.
    """
    await client.post("/api/v1/bookdrop/rescan")
    return {"status": "bookdrop rescan triggered"}


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
        log.info("starting bookorbit mcp server", transport="stdio", bookorbit_url=BASE_URL)
        mcp.run()  # stdio
    else:
        host = os.environ.get("MCP_HOST", "127.0.0.1")
        port = int(os.environ.get("MCP_PORT", "8000"))
        path = os.environ.get("MCP_PATH", "/mcp")
        log.info(
            "starting bookorbit mcp server",
            transport="http",
            host=host,
            port=port,
            path=path,
            bookorbit_url=BASE_URL,
        )
        mcp.run(transport="http", host=host, port=port, path=path)


if __name__ == "__main__":
    main()
