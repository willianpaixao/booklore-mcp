"""BookOrbitClient behavior: API-key auth, error mapping, retries.

The client is async (httpx.AsyncClient), so these tests are async too; respx
intercepts the async transport transparently.
"""

from __future__ import annotations

import httpx
import pytest

from server import BookOrbitClient, BookOrbitError

BASE = "http://bookorbit.test"
KEY = "bok_" + "k" * 49


async def test_api_key_sent_on_every_request(respx_mock):
    route = respx_mock.get(f"{BASE}/api/v1/auth/me").mock(
        return_value=httpx.Response(200, json={"username": "ann"})
    )

    client = BookOrbitClient(BASE, KEY)
    assert await client.get("/api/v1/auth/me") == {"username": "ann"}
    assert await client.get("/api/v1/auth/me") == {"username": "ann"}

    assert route.call_count == 2
    for call in route.calls:
        assert call.request.headers["X-Api-Key"] == KEY
        assert "Authorization" not in call.request.headers


async def test_caller_headers_cannot_override_api_key(respx_mock):
    route = respx_mock.get(f"{BASE}/api/v1/auth/me").mock(return_value=httpx.Response(200))

    client = BookOrbitClient(BASE, KEY)
    await client.get("/api/v1/auth/me", headers={"X-Api-Key": "other", "Accept": "x/y"})

    assert route.calls.last.request.headers["X-Api-Key"] == KEY
    assert route.calls.last.request.headers["Accept"] == "x/y"


async def test_missing_api_key_raises_without_calling_server(respx_mock):
    route = respx_mock.get(f"{BASE}/api/v1/auth/me")

    client = BookOrbitClient(BASE, None)
    with pytest.raises(BookOrbitError, match="BOOKORBIT_API_KEY"):
        await client.get("/api/v1/auth/me")
    assert not route.called


async def test_401_is_not_retried_and_explains_rejected_key(respx_mock):
    route = respx_mock.get(f"{BASE}/api/v1/auth/me").mock(
        return_value=httpx.Response(401, json={"statusCode": 401, "message": "Unauthorized"})
    )

    client = BookOrbitClient(BASE, KEY, retries=2, backoff=0)
    with pytest.raises(BookOrbitError, match="API key was rejected"):
        await client.get("/api/v1/auth/me")
    assert route.call_count == 1


@pytest.mark.parametrize(
    ("status", "code", "hint"),
    [
        (403, "API_KEY_READ_ONLY", "read-only"),
        (403, "API_KEY_ROUTE_FORBIDDEN", "does not allow API keys"),
        (401, "API_KEYS_DISABLED", "turned off"),
    ],
)
async def test_api_key_error_codes_get_hints(respx_mock, status, code, hint):
    respx_mock.post(f"{BASE}/api/v1/collections").mock(
        return_value=httpx.Response(status, json={"message": "Forbidden", "errorCode": code})
    )

    client = BookOrbitClient(BASE, KEY)
    with pytest.raises(BookOrbitError, match=hint) as excinfo:
        await client.post("/api/v1/collections", json={"name": "x"})
    assert f"[{code}]" in str(excinfo.value)


async def test_missing_permission_message_is_surfaced(respx_mock):
    respx_mock.get(f"{BASE}/api/v1/book-dock/summary").mock(
        return_value=httpx.Response(
            403, json={"statusCode": 403, "message": "Missing permission: book_dock_access"}
        )
    )

    client = BookOrbitClient(BASE, KEY)
    with pytest.raises(BookOrbitError, match="Missing permission: book_dock_access"):
        await client.get("/api/v1/book-dock/summary")


async def test_non_json_error_raises_with_status(respx_mock):
    respx_mock.get(f"{BASE}/api/v1/books/999").mock(
        return_value=httpx.Response(404, text="not found")
    )

    client = BookOrbitClient(BASE, KEY)
    with pytest.raises(BookOrbitError, match="404: not found"):
        await client.get("/api/v1/books/999")


async def test_timeout_is_wrapped_as_bookorbit_error(respx_mock):
    respx_mock.get(f"{BASE}/api/v1/libraries").mock(side_effect=httpx.ReadTimeout("slow"))

    client = BookOrbitClient(BASE, KEY)
    with pytest.raises(BookOrbitError, match="timed out"):
        await client.get("/api/v1/libraries")


async def test_transport_error_is_wrapped_as_bookorbit_error(respx_mock):
    respx_mock.get(f"{BASE}/api/v1/libraries").mock(side_effect=httpx.ConnectError("refused"))

    client = BookOrbitClient(BASE, KEY)
    with pytest.raises(BookOrbitError, match="Could not reach BookOrbit"):
        await client.get("/api/v1/libraries")


async def test_retries_transient_5xx_then_succeeds(respx_mock):
    route = respx_mock.get(f"{BASE}/api/v1/libraries").mock(
        side_effect=[httpx.Response(503), httpx.Response(200, json=[{"id": 1}])]
    )

    client = BookOrbitClient(BASE, KEY, retries=2, backoff=0)
    assert await client.get("/api/v1/libraries") == [{"id": 1}]
    assert route.call_count == 2  # one retry after the 503


async def test_hint_does_not_repeat_server_message(respx_mock):
    respx_mock.post(f"{BASE}/api/v1/collections").mock(
        return_value=httpx.Response(
            403, json={"message": "This API key is read-only", "errorCode": "API_KEY_READ_ONLY"}
        )
    )

    client = BookOrbitClient(BASE, KEY)
    with pytest.raises(BookOrbitError) as excinfo:
        await client.post("/api/v1/collections", json={"name": "x"})
    assert str(excinfo.value) == (
        "POST /api/v1/collections -> 403 [API_KEY_READ_ONLY]: This API key is read-only; "
        "use a 'Read and write' key for write tools."
    )
