"""BookLoreClient auth behavior: lazy login, 401 refresh/retry, error handling.

The client is async (httpx.AsyncClient), so these tests are async too; respx
intercepts the async transport transparently.
"""

from __future__ import annotations

import httpx
import pytest

from server import BookLoreClient, BookLoreError

BASE = "http://booklore.test"


def _login_route(respx_mock, token="acc1", refresh="ref1"):
    return respx_mock.post(f"{BASE}/api/v1/auth/login").mock(
        return_value=httpx.Response(200, json={"accessToken": token, "refreshToken": refresh})
    )


async def test_lazy_login_attaches_bearer(respx_mock):
    login = _login_route(respx_mock)
    books = respx_mock.get(f"{BASE}/api/v1/books").mock(return_value=httpx.Response(200, json=[]))

    client = BookLoreClient(BASE, "tester", "secret")
    assert await client.get("/api/v1/books") == []

    assert login.called
    assert books.calls.last.request.headers["Authorization"] == "Bearer acc1"


async def test_401_triggers_refresh_then_retries(respx_mock):
    _login_route(respx_mock, token="acc1", refresh="ref1")
    refresh = respx_mock.post(f"{BASE}/api/v1/auth/refresh").mock(
        return_value=httpx.Response(200, json={"accessToken": "acc2", "refreshToken": "ref2"})
    )
    books = respx_mock.get(f"{BASE}/api/v1/books").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=[{"id": 1}])]
    )

    client = BookLoreClient(BASE, "tester", "secret")
    assert await client.get("/api/v1/books") == [{"id": 1}]

    assert refresh.called
    assert books.calls.last.request.headers["Authorization"] == "Bearer acc2"


async def test_refresh_failure_falls_back_to_relogin(respx_mock):
    login = respx_mock.post(f"{BASE}/api/v1/auth/login").mock(
        side_effect=[
            httpx.Response(200, json={"accessToken": "acc1", "refreshToken": "ref1"}),
            httpx.Response(200, json={"accessToken": "acc3", "refreshToken": "ref3"}),
        ]
    )
    refresh = respx_mock.post(f"{BASE}/api/v1/auth/refresh").mock(return_value=httpx.Response(401))
    books = respx_mock.get(f"{BASE}/api/v1/books").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json=[])]
    )

    client = BookLoreClient(BASE, "tester", "secret")
    assert await client.get("/api/v1/books") == []

    assert refresh.called
    assert login.call_count == 2
    assert books.calls.last.request.headers["Authorization"] == "Bearer acc3"


async def test_missing_credentials_raises():
    client = BookLoreClient(BASE, None, None)
    with pytest.raises(BookLoreError, match="BOOKLORE_USERNAME"):
        await client.get("/api/v1/books")


async def test_http_error_raises(respx_mock):
    _login_route(respx_mock)
    respx_mock.get(f"{BASE}/api/v1/books/999").mock(
        return_value=httpx.Response(404, text="not found")
    )

    client = BookLoreClient(BASE, "tester", "secret")
    with pytest.raises(BookLoreError, match="404"):
        await client.get("/api/v1/books/999")


async def test_timeout_is_wrapped_as_booklore_error(respx_mock):
    _login_route(respx_mock)
    respx_mock.get(f"{BASE}/api/v1/books").mock(side_effect=httpx.ReadTimeout("slow"))

    client = BookLoreClient(BASE, "tester", "secret")
    with pytest.raises(BookLoreError, match="timed out"):
        await client.get("/api/v1/books")


async def test_transport_error_is_wrapped_as_booklore_error(respx_mock):
    _login_route(respx_mock)
    respx_mock.get(f"{BASE}/api/v1/books").mock(side_effect=httpx.ConnectError("refused"))

    client = BookLoreClient(BASE, "tester", "secret")
    with pytest.raises(BookLoreError, match="Could not reach BookLore"):
        await client.get("/api/v1/books")


async def test_retries_transient_5xx_then_succeeds(respx_mock):
    _login_route(respx_mock)
    route = respx_mock.get(f"{BASE}/api/v1/books").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=[{"id": 1}])]
    )

    client = BookLoreClient(BASE, "tester", "secret", retries=2, backoff=0)
    assert await client.get("/api/v1/books") == [{"id": 1}]
    assert route.call_count == 2  # one retry after the 503


async def test_book_list_cache_served_then_invalidated_on_write(respx_mock):
    _login_route(respx_mock)
    books = respx_mock.get(f"{BASE}/api/v1/books").mock(
        return_value=httpx.Response(200, json=[{"id": 1}])
    )
    respx_mock.post(f"{BASE}/api/v1/books/status").mock(return_value=httpx.Response(200, json=[]))

    client = BookLoreClient(BASE, "tester", "secret", cache_ttl=30)
    await client.get_books()
    await client.get_books()
    assert books.call_count == 1  # second call served from cache

    await client.post("/api/v1/books/status", json={"bookIds": [1], "status": "READ"})
    await client.get_books()
    assert books.call_count == 2  # write invalidated the cache
