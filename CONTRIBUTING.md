# Contributing

Thanks for helping improve the BookLore MCP server! It's a single-file FastMCP
server (`server.py`) plus a logging module (`log.py`), with tests under `tests/`.

## Setup

Uses [uv](https://docs.astral.sh/uv/). `uv sync` creates the virtualenv and installs
runtime + dev dependencies.

```bash
uv sync
cp .env.example .env   # add your BookLore URL + credentials (used for live testing)
```

## Dev loop

Run all four before opening a PR — CI runs the same across Python 3.10–3.13:

```bash
uv run ruff check .         # lint
uv run ruff format .        # format (use --check in CI)
uv run mypy                 # type check (server.py, log.py)
uv run pytest               # tests — fully offline, no live BookLore needed
```

## Project layout

| File | What's in it |
|---|---|
| `server.py` | The `BookLoreClient` (auth, retry, caching) + all `@mcp.tool` definitions |
| `log.py` | structlog configuration (`configure_logging`, `get_logger`) |
| `tests/` | `test_client.py` (HTTP client), `test_tools.py` / `test_search.py` (tools), `test_helpers.py`, `test_logging.py` |

## Adding a tool

Tools are `async` functions decorated with `@mcp.tool`. Use full type annotations
(they become the input/output schema), a docstring (it's the tool description), and
the right behavior hints. Call the shared `client` for HTTP.

```python
@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Count books"})
async def count_books() -> dict:
    """Return the total number of books in the library."""
    books = await client.get_books()
    return {"count": len(books)}
```

Conventions:
- **Annotations:** mark reads `readOnlyHint=True`; mark anything that overwrites or
  deletes `destructiveHint=True`; add `idempotentHint=True` when re-running is safe.
- **Errors:** raise `BookLoreError` (a `ToolError` subclass) for user-facing failures
  — transport/timeout errors are already wrapped for you.
- **Reads over the whole library:** use `client.get_books()` (it's cached), not a raw
  `client.get("/api/v1/books")`.
- **Prefer additive writes** (merge) over `REPLACE_ALL`. See `_apply_patch`.
- Keep line length ≤ 100 and let `ruff format` handle style.

## Testing

Tests are offline: [`respx`](https://lundberg.github.io/respx/) mocks the BookLore
API and FastMCP's in-memory `Client` drives the tools. Pattern:

```python
async def test_count_books(authed):  # `authed` fixture mocks login + a fresh client
    authed.get("http://booklore.test/api/v1/books").mock(
        return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}])
    )
    async with Client(server.mcp) as c:
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
