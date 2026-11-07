"""BookLore HTTP client: a thin authenticated wrapper over the BookLore REST API.

Logs in with BOOKLORE_USERNAME / BOOKLORE_PASSWORD to obtain a JWT access + refresh
token pair, attaches `Authorization: Bearer`, refreshes on a 401, retries transient
transport/5xx failures with backoff, and serves a short-TTL cache of the full book list.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from fastmcp.exceptions import ToolError

from .log import get_logger

log = get_logger("booklore")


class BookLoreError(ToolError):
    """A BookLore API or transport error surfaced to the MCP client.

    Subclasses FastMCP's ToolError so its message is sent to the LLM (ordinary
    exceptions are masked as a generic internal error)."""


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
        The cache is invalidated automatically on any write to /api/v1/books.

        The full (withDescription) list is a superset of the light one, so a fresh full
        list also answers a light request instead of triggering a second download."""
        if self._cache_ttl > 0:
            for key in (with_description, True):
                hit = self._books_cache.get(key)
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
