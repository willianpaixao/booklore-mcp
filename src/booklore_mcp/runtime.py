"""The shared BookLore client singleton.

`client` is a *mutable module attribute* on purpose. Everything that talks to BookLore
must reach it through this module — `from . import runtime` (or `from .. import runtime`
in the tools package) and then `runtime.client.<method>()` — and must NOT do
`from .runtime import client`. The test suite swaps `runtime.client` wholesale to reset
auth/cache state between tests; a direct name-import would bind the old object and never
see the swap.
"""

from __future__ import annotations

from . import config
from .client import BookLoreClient

client = BookLoreClient(
    config.BASE_URL,
    config.USERNAME,
    config.PASSWORD,
    timeout=config.TIMEOUT,
    retries=config.RETRIES,
    backoff=config.BACKOFF,
    cache_ttl=config.CACHE_TTL,
)
