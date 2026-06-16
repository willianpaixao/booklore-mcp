"""Shared test setup.

Sets fake BookLore credentials in the environment *before* the server module is
imported (it reads them at import time to build the module-level client), so the
whole suite runs offline against respx-mocked HTTP — no live BookLore needed.
"""

from __future__ import annotations

import os

# Base URL the mocked routes are registered against. Force-set (not setdefault) so
# a real BOOKLORE_URL in the ambient environment can't leak into the test process.
BASE = "http://booklore.test"
os.environ["BOOKLORE_URL"] = BASE
os.environ["BOOKLORE_USERNAME"] = "tester"
os.environ["BOOKLORE_PASSWORD"] = "secret"
