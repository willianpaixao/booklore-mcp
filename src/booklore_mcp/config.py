"""Configuration for the BookLore MCP server, read from the environment.

Imported for its side-effect-free module-level constants; everything is resolved
once at import time. Tests set the relevant variables *before* importing the package
(see tests/conftest.py).
"""

from __future__ import annotations

import os
from typing import Literal

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
CACHE_TTL = float(os.environ.get("BOOKLORE_CACHE_TTL", "60"))
# Max concurrent per-book operations in bulk_update_metadata. Defaults to 1
# (sequential): BookLore stores tags/categories as shared rows with a UNIQUE name,
# so concurrently adding the SAME new tag to several books trips a data-conflict
# (HTTP 400). Raise this only when per-book patches don't share new tags/categories.
BULK_CONCURRENCY = int(os.environ.get("BOOKLORE_BULK_CONCURRENCY", "1"))

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
