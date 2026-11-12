# Contributing

Thanks for helping improve the BookOrbit MCP server! It's a single-file FastMCP server
(`server.py`) plus a logging module (`log.py`), with tests under `tests/`. It targets
BookOrbit v3.8.0.

## Setup

Uses [uv](https://docs.astral.sh/uv/). `uv sync` creates the virtualenv and installs
runtime + dev dependencies.

```bash
uv sync
cp .env.example .env   # add your BookOrbit URL + API key
```

## Dev loop

Run all four before opening a PR — CI runs the same across Python 3.12–3.14:

```bash
uv run ruff check .         # lint
uv run ruff format .        # format (use --check in CI)
uv run mypy                 # type check (server.py, log.py)
uv run pytest               # tests — offline, no live BookOrbit needed
```

## Project layout

| File | What's in it |
|---|---|
| `server.py` | `BookOrbitClient` (API-key auth, retries, error mapping), the book-query builders and validation, and every `@mcp.tool` |
| `log.py` | structlog configuration (`configure_logging`, `get_logger`) |
| `tests/conftest.py` | Fake URL + API key, and the `authed` fixture (a fresh client per test) |
| `tests/test_client.py` | HTTP client: auth header, error codes and hints, retries |
| `tests/test_query.py` | Query building and validation, query routes, paging, book summaries |
| `tests/test_read_tools.py` | Search, listing, metadata lookup, duplicates and `ping` tools |
| `tests/test_write_tools.py` | Single-book write tools: metadata, tags/genres, locks, status, collections, progress |
| `tests/test_bulk_tools.py` | `bulk_update_metadata`, `bulk_set_read_status`, library scan, book dock |
| `tests/test_insight_tools.py` | Series, smart scopes, statistics, reading history, annotations, metadata refresh |
| `tests/test_logging.py` | Logging configuration |
| `tests/integration/` | Opt-in, read-only tests against a live server |

## Adding a tool

Tools are `async` functions decorated with `@mcp.tool`. Use full type annotations
(they become the input/output schema), a docstring (it's the tool description), and the
right behavior hints. Call the shared `client` for HTTP.

```python
@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True, "title": "Count books"})
async def count_books() -> dict:
    """Return the total number of books in the library."""
    summary = await client.get("/api/v1/statistics/summary")
    return {"count": summary["totalBooks"]}
```

Conventions:

- **Annotations:** mark reads `readOnlyHint=True`; mark anything that overwrites or
  deletes `destructiveHint=True`; add `idempotentHint=True` when re-running is safe.
- **Errors:** raise `BookOrbitError` (a `ToolError` subclass) for user-facing failures.
  Transport/timeout errors are already wrapped, and API errors carry BookOrbit's
  `errorCode` and message (e.g. the missing permission).
- **Never load the whole library.** BookOrbit is built for libraries of tens of
  thousands of books. Filter and sort on the server: build queries with `rule`,
  `all_of` / `any_of`, `sort_by` and `build_book_query`, and fetch with
  `_books_window` (an offset/limit window) or `client.iter_book_pages` (bounded paging).
- **Validate before sending.** The server rejects unknown body fields, and a malformed
  book query comes back as a bare 400, so check inputs client-side and fail with a
  message that says what is wrong. Field and operator tables mirror the v3.8.0 source;
  keep them in sync when targeting a new BookOrbit version.
- **Prefer BookOrbit's bulk routes** (e.g. `bulk-edit-metadata`) over per-book loops,
  and merging over replacing.
- **Document the key it needs.** If a tool needs Read-and-write access or a permission
  (`library_edit_metadata`, `manage_libraries`, …), say so in its docstring and in the
  README's **Key** column.
- Keep line length ≤ 100 and let `ruff format` handle style.

## Testing

Tests are offline: [`respx`](https://lundberg.github.io/respx/) mocks the BookOrbit API
and FastMCP's in-memory `Client` drives the tools. Pattern:

```python
async def test_count_books(authed):  # `authed` gives a fresh client with a test API key
    authed.get("http://bookorbit.test/api/v1/statistics/summary").mock(
        return_value=httpx.Response(200, json={"totalBooks": 2})
    )
    async with Client(server.mcp) as c:
        result = await c.call_tool("count_books", {})
    assert result.data["count"] == 2
```

Make all calls of one test inside a single `Client(server.mcp)` session: the server's
lifespan closes the shared HTTP client when a session ends.

For pure helpers (e.g. `_summarize_card`, `build_book_query`), test the function
directly — no mocking needed. Add at least one test per new tool or helper, including
the request body it sends.

### Live integration tests (opt-in)

`tests/integration/` holds read-only tests against a real BookOrbit server. They are
skipped unless `BOOKORBIT_IT_URL` is set, so the default run (and CI) stays offline.
Use a read-only API key:

```bash
BOOKORBIT_IT_URL=https://books.example.com \
BOOKORBIT_IT_API_KEY=bok_... \
uv run pytest -m integration
```

Integration tests must never write to the server. Check write tools against a separate
test library (with file writing turned off), never against books you care about.

## Pull requests

- Branch off `main`; keep changes focused.
- Make sure lint, format, mypy, and tests all pass.
- If you add or change behavior, update the README tool table (including the **Key**
  column) and any relevant env vars, and add tests.
- Don't commit secrets — `.env` is gitignored; pass the API key via the environment.
