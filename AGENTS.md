# AGENTS.md

Guidance for coding agents (Claude Code and other agent tools) working in this
repository. Human contributors: see [README.md](README.md) and
[CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

A local [MCP](https://modelcontextprotocol.io) server that wraps a self-hosted
**BookLore** instance's REST API (`/api/v1/...`) so an MCP host (Claude Desktop/Code)
can search a library, read and edit metadata, manage shelves, and track reading
progress. It is **single-user**: it logs in with the owner's BookLore credentials
(JWT access + refresh, refreshed automatically on a 401). It runs as a long-lived
**HTTP** server by default, or over **stdio** (`MCP_TRANSPORT=stdio`) for local hosts
that launch it directly.

## Repo layout

```
src/booklore_mcp/        # all application code (src layout)
  config.py              # env-var configuration + ReadStatus literal
  log.py                 # structlog setup (configure_logging / get_logger)
  client.py              # BookLoreClient (auth, retry, cache) + BookLoreError
  runtime.py             # the shared, swappable `client` singleton
  models.py              # Pydantic response models (BookSummary, SearchResult)
  helpers.py             # shared helpers (filtering, metadata patch, summaries)
  app.py                 # the FastMCP instance `mcp`, lifespan, main()
  tools/                 # the MCP tools, grouped by theme (one module each)
    books.py shelves.py reading.py metadata.py search.py series.py stats.py libraries.py
  prompts.py             # @mcp.prompt workflows (e.g. organize_library)
server.py                # thin backwards-compat launcher (exposes `mcp`, runs main())
tests/                   # pytest suite (offline, respx-mocked HTTP)
Dockerfile .dockerignore pyproject.toml uv.lock   # build / packaging
.env.example README.md CONTRIBUTING.md LICENSE    # docs / sample config
```

Key internals to know before editing:

- **The `client` singleton lives in `runtime.py` and is mutable.** Tools and helpers
  must reference it as `runtime.client.<method>()` (via `from . import runtime` /
  `from .. import runtime`), **never** `from .runtime import client`. The test suite
  swaps `runtime.client` wholesale to reset auth/cache state between tests, and a
  direct name-import would not see the swap.
- **Tools and prompts register by import side-effect.** `tools/__init__.py` imports every
  tool module (each decorates functions with `@mcp.tool`), and the package `__init__.py`
  imports `prompts`. A new tool module must be added to `tools/__init__.py`, and new
  tools to `EXPECTED_TOOLS` in `tests/test_deployment.py`.
- **Writes that an LLM plans go through a preview.** Follow the `dry_run=True` default
  pattern (`normalize_goodreads_ids`, `organize_shelves`): preview, let the user approve,
  then apply. Keep such writes additive. `app.py` must not import `tools`
  (that would create a cycle) — the package `__init__.py` / `__main__.py` pulls them in.
- Module layering is one-directional:
  `config,log,models → client → runtime → helpers → app → tools/*`.

## Running checks

Run the full gate before proposing changes (CI runs the same on 3.12–3.14):

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest
```

Smoke-test tool registration and discovery via the compat shim:

```bash
uv run fastmcp list server.py       # should list every tool
uv run server.py                    # starts the HTTP server (Ctrl-C to stop)
python -m booklore_mcp              # equivalent module entrypoint
```

## Security & access-control best practices (MCP / API services)

- **Secrets come only from the environment.** `BOOKLORE_USERNAME` / `BOOKLORE_PASSWORD`
  are read from env or a local `.env` (git-ignored). Never hardcode, commit, or log
  credentials; `.env.example` holds placeholders only. `.claude/settings.json` denies
  agent reads of `.env`, keys, and secret files.
- **Bind to loopback by default.** The HTTP transport defaults to `127.0.0.1`. Expose it
  beyond localhost only behind a reverse proxy or equivalent network controls (the
  Docker image binds `0.0.0.0` on purpose — the container's published port and proxy
  gate exposure).
- **Least privilege & safe writes.** The server acts with the BookLore user's full
  rights. Keep destructive tools annotated with `destructiveHint` (and `idempotentHint`
  where the operation is safe to repeat), validate IDs/inputs before write calls, and
  prefer additive operations (e.g. `add_to_shelves`) over wholesale replacement.
- **Don't leak auth in logs.** Keep timeouts and retries bounded (`BOOKLORE_TIMEOUT`,
  `BOOKLORE_RETRIES`, `BOOKLORE_BACKOFF`); never log bearer tokens or request bodies
  that carry them.
- **Supply chain & runtime.** Keep `uv.lock` pinned; the CI `pip-audit` job scans
  dependencies. The container runs as the non-root `app` user — keep it that way.
- **Security audits.** `.claude/agents/security-auditor.md` is a read-only Claude Code
  subagent that runs the local scanners and audits the areas above, then reports ranked
  findings. Ask Claude Code to "use the security-auditor agent" on the repo or on a change.

## Documentation & information best practices

- **Tool docstrings are model-facing context, not just human docs.** Each tool's
  docstring, `title`, and annotations become the description an LLM reads to decide how
  to call it — keep them accurate, specific, and unambiguous, and update them in the
  same change that alters behavior.
- **Keep docs in sync with code.** When you add/rename/remove a tool or env var, update
  the README tool table, this file, and `CONTRIBUTING.md` together.
- **Document configuration.** Every env var should have a documented purpose and default
  (see `config.py` and the README environment table).
- **Logging over prints.** Use the structured logger from `log.py`; everything goes to
  **stderr** so the stdio transport's stdout stays a clean JSON-RPC stream.
