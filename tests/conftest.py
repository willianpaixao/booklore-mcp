"""Shared test setup.

Sets a fake BookOrbit URL and API key in the environment *before* the server module
is imported (it reads them at import time to build the module-level client), so the
whole suite runs offline against respx-mocked HTTP — no live BookOrbit needed.
"""

from __future__ import annotations

import os

import pytest

# Base URL the mocked routes are registered against. Force-set (not setdefault) so
# a real BOOKORBIT_URL / BOOKORBIT_API_KEY in the ambient environment can't leak
# into the test process.
BASE = "http://bookorbit.test"
TEST_API_KEY = "bok_" + "t" * 49
os.environ["BOOKORBIT_URL"] = BASE
os.environ["BOOKORBIT_API_KEY"] = TEST_API_KEY


@pytest.fixture
def authed(respx_mock):
    """Give each test a fresh client with the test API key, plus the respx router.

    The server's lifespan closes the shared HTTP client when the in-memory
    `Client(mcp)` context exits, so we reinstate a fresh client per test rather
    than reusing a closed one.
    """
    import server

    server.client = server.BookOrbitClient(BASE, TEST_API_KEY)
    return respx_mock
