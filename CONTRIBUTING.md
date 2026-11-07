# Contributing

Thanks for helping improve the BookLore MCP server! The code lives in the
`src/booklore_mcp/` package — core modules (client, config, helpers, models, logging)
plus a `tools/` subpackage grouping the `@mcp.tool` definitions by theme — with tests
under `tests/`. A thin root `server.py` keeps `uv run server.py` and `fastmcp … server.py`
working. See [AGENTS.md](AGENTS.md) for an orientation to the layout and conventions.

## Setup

Uses [uv](https://docs.astral.sh/uv/). `uv sync` creates the virtualenv and installs
runtime + dev dependencies.

```bash
uv sync
cp .env.example .env   # add your BookLore URL + credentials (used for live testing)
```

## Dev loop

Run all four before opening a PR — CI runs the same across Python 3.12–3.14:

```bash
uv run ruff check .         # lint
uv run ruff format .        # format (use --check in CI)
uv run mypy                 # type check (src/booklore_mcp + the server.py shim)
uv run pytest               # tests — fully offline, no live BookLore needed
```

## Project layout

| Path | What's in it |
|---|---|
| `src/booklore_mcp/client.py` | The `BookLoreClient` (auth, retry, caching) + `BookLoreError` |
| `src/booklore_mcp/runtime.py` | The shared, swappable `client` singleton |
| `src/booklore_mcp/config.py` | Env-var configuration + the `ReadStatus` literal |
| `src/booklore_mcp/log.py` | structlog configuration (`configure_logging`, `get_logger`) |
| `src/booklore_mcp/models.py` | Pydantic response models (`BookSummary`, `SearchResult`) |
| `src/booklore_mcp/helpers.py` | Shared helpers (filtering, metadata patching, summaries) |
| `src/booklore_mcp/app.py` | The `FastMCP` instance `mcp`, lifespan, and `main()` |
| `src/booklore_mcp/tools/` | The `@mcp.tool` definitions, grouped: `books`, `shelves`, `reading`, `metadata`, `search`, `series`, `stats`, `libraries` |
| `src/booklore_mcp/prompts.py` | `@mcp.prompt` workflows (e.g. `organize_library`) |
| `server.py` | Thin backwards-compat launcher (exposes `mcp`, runs `main()`) |
| `tests/` | `test_client.py` (HTTP client), `test_tools.py` / `test_search.py` / `test_series.py` (tools), `test_deployment.py` (registration + entrypoints), `test_helpers.py`, `test_logging.py` |

## Adding a tool

Add tools to the themed module under `src/booklore_mcp/tools/` that fits best (or a
new module, imported from `tools/__init__.py` so it registers). Tools are `async`
functions decorated with `@mcp.tool`. Use full type annotations (they become the
input/output schema), a docstring (it's the tool description), and the right behavior
hints. Reach the shared client via `runtime.client` — **never** `from ..runtime import
client` (the tests swap the singleton; a name-import wouldn't see the swap).

```python
from .. import runtime
from ..app import mcp


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Count books"})
async def count_books() -> dict:
    """Return the total number of books in the library."""
    books = await runtime.client.get_books()
    return {"count": len(books)}
```

Conventions:
- **Annotations:** mark reads `readOnlyHint=True`; mark anything that overwrites or
  deletes `destructiveHint=True`; add `idempotentHint=True` when re-running is safe.
- **Errors:** raise `BookLoreError` (a `ToolError` subclass) for user-facing failures
  — transport/timeout errors are already wrapped for you.
- **Reads over the whole library:** use `runtime.client.get_books()` (it's cached), not
  a raw `runtime.client.get("/api/v1/books")`.
- **Prefer additive writes** (merge) over `REPLACE_ALL`. See `_apply_patch`.
- Keep line length ≤ 100 and let `ruff format` handle style.

## Testing

Tests are offline: [`respx`](https://lundberg.github.io/respx/) mocks the BookLore
API and FastMCP's in-memory `Client` drives the tools. Pattern:

```python
from booklore_mcp.app import mcp


async def test_count_books(authed):  # `authed` fixture mocks login + a fresh client
    authed.get("http://booklore.test/api/v1/books").mock(
        return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}])
    )
    async with Client(mcp) as c:
        result = await c.call_tool("count_books", {})
    assert result.data["count"] == 2
```

For pure helpers (e.g. `_summarize_book`, `_matches_filters`), test the function
directly — no mocking needed. Add at least one test per new tool or helper.

Optionally smoke-test against a real instance (loads `.env`):

```bash
set -a && source .env && set +a
uv run fastmcp call server.py search_books --input-json '{"filters": {"missing": ["tags"]}}'
```

## Pull requests

- Branch off `main`; keep changes focused.
- Make sure lint, format, mypy, and tests all pass.
- If you add or change behavior, update the README tool table and any relevant env
  vars, and add tests.
- Don't commit secrets — `.env` is gitignored; pass credentials via the environment.
