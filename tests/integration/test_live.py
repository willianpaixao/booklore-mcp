"""Opt-in, read-only checks against a real BookOrbit server.

Skipped unless BOOKORBIT_IT_URL is set. They use their own variables because
tests/conftest.py forces BOOKORBIT_URL to a fake host for the offline suite.

    BOOKORBIT_IT_URL=https://books.example.com \\
    BOOKORBIT_IT_API_KEY=bok_... \\
    uv run pytest -m integration

These tests must only ever read: never add a test that writes to the server.
"""

from __future__ import annotations

import os

import pytest

from server import BookOrbitClient

IT_URL = os.environ.get("BOOKORBIT_IT_URL", "").rstrip("/")
IT_API_KEY = os.environ.get("BOOKORBIT_IT_API_KEY", "").strip()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not IT_URL, reason="set BOOKORBIT_IT_URL to run live tests"),
]

needs_key = pytest.mark.skipif(not IT_API_KEY, reason="set BOOKORBIT_IT_API_KEY")


async def test_health_is_up():
    client = BookOrbitClient(IT_URL, None, timeout=30)
    try:
        resp = await client._send("GET", "/api/v1/health")
    finally:
        await client.close()
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@needs_key
async def test_api_key_authenticates():
    client = BookOrbitClient(IT_URL, IT_API_KEY, timeout=30)
    try:
        me = await client.get("/api/v1/auth/me")
    finally:
        await client.close()
    assert me["authenticationMethod"] == "api_key"
