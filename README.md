# BookLore MCP server

A local [MCP](https://modelcontextprotocol.io) server that wraps a self-hosted
**BookLore** instance's REST API so Claude can search your library, read and edit
metadata, manage shelves, and track reading progress.

Runs as a long-lived **HTTP** server (default) or over **stdio** (for local Claude
launches), and talks to your BookLore over HTTP. Single-user — authenticates with
your BookLore login (JWT, auto-refreshed).

## Tools

| Tool | Kind | What it does |
|---|---|---|
| `search_books` | read | Search/filter the library (tags, categories, authors, shelves, **missing fields**), sorted + paginated |
| `list_books` | read | List/search books (trimmed summaries) |
| `get_book` | read | Full record + metadata for one book |
| `list_shelves` | read | All shelves |
| `get_shelf_books` | read | Books on a shelf |
| `get_recommendations` | read | Books similar to a given one |
| `list_authors` | read | Distinct authors with per-author book counts |
| `books_by_author` | read | Books by a given author |
| `get_reading_stats` | read | Counts by read status + average rating |
| `export_library` | read | Export the library (or a shelf) as JSON or CSV; `fields` projection or `full=True` for ISBN/ASIN/ratings/IDs |
| `find_duplicates` | read | Group duplicate books via BookLore's native detection (presets + per-signal control) |
| `list_libraries` | read | List libraries and their on-disk paths (id, name, watch, paths, allowed formats) |
| `refresh_library` | write | Rescan a library's paths so BookLore ingests files added on disk |
| `bookdrop_rescan` | write | Rescan BookLore's bookdrop folder for newly added files (staged for review) |
| `ping` | read | Liveness + auth probe (down vs. logged-out), server version, counts |
| `isbn_lookup` | read | Fetch metadata for an ISBN from external providers |
| `fetch_metadata_candidates` | read | Fetch candidate metadata from external providers (review, then apply); per-provider status |
| `add_tags` / `remove_tags` | write | Add/remove tags on a book (merge, idempotent) |
| `add_categories` / `remove_categories` | write | Add/remove categories (merge, idempotent) |
| `bulk_update_metadata` | write | Apply per-book patches to many books — tags/categories, arbitrary fields, clears (partial-success) |
| `bulk_set_read_status` | write | Set per-book reading status in bulk (e.g. a Goodreads import), grouped by status |
| `set_field_locks` | write | Lock/unlock metadata fields so curated values survive a refresh |
| `update_book_metadata` | write | Edit a book's metadata (field-merge by default; `clear_fields` to null a field; REPLACE_ALL to wipe omitted) |
| `set_read_status` | write | Set reading status (READING/READ/…) |
| `set_personal_rating` | write | Set your rating |
| `assign_shelves` | write | Set books' shelves (add and/or remove) |
| `add_to_shelves` / `remove_from_shelves` | write | Additive shelf membership (idempotent) |
| `create_shelf` | write | Create a shelf |
| `delete_shelf` | write | Delete a shelf (destructive) |
| `set_reading_progress` | write | Record reading position / % for a book file |
| `reset_progress` | write | Clear reading progress (destructive) |

For bulk tagging/enrichment, prefer `search_books` (especially the `missing` filter —
now covering identifier/rating fields like `asin`/`goodreadsId`/`amazonRating`, with a
`missing_mode: any` for "lacking at least one") to find un-enriched books, plus the
additive `add_tags`/`add_categories` and `bulk_update_metadata` — they merge by default
and touch only the fields they manage. `update_book_metadata` defaults to
`REPLACE_WHEN_PROVIDED` (writes only the fields you include, leaving the rest intact) —
use that for fill-in enrichment; pass `clear_fields` to null a single field without
touching the others. Its `REPLACE_ALL` mode replaces the **whole** record (any field you
omit is wiped to null), so reserve it for deliberate full-record edits, and lock curated
fields with `set_field_locks` first.

`fetch_metadata_candidates` and `isbn_lookup` depend on BookLore's external metadata
providers (Google, GoodReads, Amazon, …) being reachable and configured on your server.
`fetch_metadata_candidates` defaults its search terms from the book's own stored
metadata (so `book_id` alone works) and returns a `provider_status` for each requested
provider — `ok` / `empty` / `disabled` — so a dead or unconfigured provider (e.g. Amazon
without a session cookie) is distinguishable from a genuine no-match. Each candidate
carries only its own provider's fields (Amazon → `asin`/`amazonRating`, GoodReads →
`goodreadsId`/`goodreadsRating`, Google → `googleId`), so include the provider that owns
the field you want to fill.

## Setup

Uses [uv](https://docs.astral.sh/uv/). `uv sync` creates the virtualenv and
installs dependencies; `uv run` auto-syncs before running, so you never have to
activate anything.

```bash
cd booklore-mcp
uv sync                   # creates .venv, installs fastmcp + httpx
cp .env.example .env      # then edit with your BookLore URL + credentials
```

## Configure credentials

The server reads three environment variables (see `.env.example`):

- `BOOKLORE_URL` — your BookLore base URL (default `http://localhost:6060`)
- `BOOKLORE_USERNAME`
- `BOOKLORE_PASSWORD`
- `BOOKLORE_TIMEOUT` — per-request timeout in seconds (default `120`). Metadata
  writes regenerate covers server-side and can be slow; raise this if you hit
  timeouts on tags/metadata updates.
- `BOOKLORE_RETRIES` — retries for transient failures (timeouts, 429/5xx) with
  exponential backoff (default `2`); `BOOKLORE_BACKOFF` sets the base delay in
  seconds (default `0.5`).
- `BOOKLORE_CACHE_TTL` — seconds to cache the full book list, shared by the
  search/stats/export tools (default `10`; `0` disables). Any write to a book
  invalidates it immediately.
- `BOOKLORE_BULK_CONCURRENCY` — max concurrent per-book operations in
  `bulk_update_metadata` (default `1`, i.e. sequential). BookLore stores tags and
  categories as shared rows with a unique name, so adding the *same* new tag to
  several books in parallel causes a data-conflict (HTTP 400). Raise this only when
  your per-book patches don't introduce the same new tags/categories.

## Logging

Logging is handled by [structlog](https://www.structlog.org/) and configured with
two environment variables:

- `LOG_LEVEL` — `DEBUG`, `INFO` (default), `WARNING`, `ERROR`, or `CRITICAL`
- `LOG_FORMAT` — `console` (default; human-readable, coloured on a TTY) or `json`
  (one JSON object per line, for log aggregators)

Logs are written to **stderr** so they never interfere with the MCP JSON-RPC stream
on stdout. Both this server's logs and those of its libraries (httpx, uvicorn, …)
share the same format. The Docker image defaults to `LOG_FORMAT=json`.

```bash
LOG_LEVEL=DEBUG LOG_FORMAT=json uv run server.py
```

## Run standalone

The transport is selected with `MCP_TRANSPORT` (`http` by default).

### HTTP (long-lived server, default)

```bash
set -a && source .env && set +a
uv run server.py
```

Serves streamable-HTTP MCP at `http://127.0.0.1:8000/mcp`. Override with
`MCP_HOST`, `MCP_PORT`, `MCP_PATH`. Press Ctrl-C to stop. `uv run booklore-mcp`
(the installed console script) is equivalent to `uv run server.py`.

> **Security:** this endpoint has no auth of its own and acts with your BookLore
> credentials, so anyone who can reach the port controls your library. Keep it
> bound to `127.0.0.1` (the default). Only set `MCP_HOST=0.0.0.0` behind a
> reverse proxy that adds TLS + authentication.

### stdio (local process)

```bash
set -a && source .env && set +a
MCP_TRANSPORT=stdio uv run server.py
```

Speaks MCP over stdin/stdout, so on its own it just blocks waiting for a client —
this is the form an MCP host launches directly (see Claude Code/Desktop below).

## Run with Docker

The image runs the HTTP transport bound to `0.0.0.0` inside the container; you
control exposure with the published port. Credentials are passed in at runtime, so
they're never baked into the image.

```bash
docker build -t booklore-mcp .
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 booklore-mcp
```

`--env-file .env` loads your credentials from the `.env` file (same one from
[Configure credentials](#configure-credentials)) — each `KEY=value` line becomes an
environment variable in the container:

```bash
# .env
BOOKLORE_URL=http://host.docker.internal:6060
BOOKLORE_USERNAME=you
BOOKLORE_PASSWORD=secret
```

The path is relative to where you run the command (`--env-file ./path/to/.env` for
another location). To pass variables individually instead of a file, use `-e`:

```bash
docker run --rm \
  -e BOOKLORE_URL=http://host.docker.internal:6060 \
  -e BOOKLORE_USERNAME=you \
  -e BOOKLORE_PASSWORD=secret \
  -p 127.0.0.1:8000:8000 booklore-mcp
```

The MCP endpoint is served at `http://127.0.0.1:8000/mcp`.

> **Security:** the publish mapping above binds to `127.0.0.1` on purpose — the
> endpoint has no auth of its own and acts with your BookLore credentials. If your
> BookLore runs in its own container, point `BOOKLORE_URL` at it over a shared Docker
> network (or `host.docker.internal`) rather than `localhost`. Only expose the port
> beyond loopback behind a reverse proxy that adds TLS + authentication.

## Test it

Everything here is pure Python — no Node/npm required.

List the tools and their schemas:

```bash
uv run fastmcp list server.py
```

Call a tool for a live check (load your env first so it authenticates):

```bash
set -a && source .env && set +a
uv run fastmcp call server.py list_books query=tolkien
```

Arguments are `key=value` pairs; use `--input-json '{...}'` for nested ones. Both
commands also accept a URL (e.g. `http://127.0.0.1:8000/mcp`) instead of
`server.py` to test a running HTTP server.

## Add to Claude Code

**HTTP (default)** — start the server (see [Run as an HTTP server](#http-long-lived-server-default)),
then connect to it:

```bash
claude mcp add --transport http booklore http://127.0.0.1:8000/mcp
```

**Local (stdio)** — Claude launches the process itself (note `MCP_TRANSPORT=stdio`):

```bash
claude mcp add booklore \
  --scope user \
  --env MCP_TRANSPORT=stdio \
  --env BOOKLORE_URL=http://localhost:6060 \
  --env BOOKLORE_USERNAME=you \
  --env BOOKLORE_PASSWORD=secret \
  -- uv --directory /absolute/path/to/booklore-mcp run server.py
```

`uv --directory <path> run` resolves the project's environment automatically, so
`fastmcp` and `httpx` are always available.

## Add to Claude Desktop

Claude Desktop launches the process, which must speak stdio — so pin
`MCP_TRANSPORT=stdio`. Edit `claude_desktop_config.json` (Settings → Developer →
Edit Config):

```json
{
  "mcpServers": {
    "booklore": {
      "command": "uv",
      "args": ["--directory", "/abs/path/to/booklore-mcp", "run", "server.py"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "BOOKLORE_URL": "http://localhost:6060",
        "BOOKLORE_USERNAME": "you",
        "BOOKLORE_PASSWORD": "secret"
      }
    }
  }
}
```

## Use with Open WebUI (OpenAPI)

Open WebUI consumes **OpenAPI tool servers**, not MCP directly. Bridge to it with
[`mcpo`](https://github.com/open-webui/mcpo), the MCP→OpenAPI proxy: it wraps this
server and exposes every tool as a REST endpoint plus an auto-generated
`/openapi.json` and `/docs`.

With the server already running over HTTP (the default):

```bash
# server on :8000/mcp, proxy serving OpenAPI on :9000
uvx mcpo --port 9000 --server-type streamable-http -- http://127.0.0.1:8000/mcp
```

Or let `mcpo` launch and manage the server itself over stdio (no separate process
to run — pass the BookLore env through with `-e`):

```bash
uvx mcpo --port 9000 \
  -e MCP_TRANSPORT=stdio \
  -e BOOKLORE_URL=http://localhost:6060 \
  -e BOOKLORE_USERNAME=you \
  -e BOOKLORE_PASSWORD=secret \
  -- uv --directory /abs/path/to/booklore-mcp run server.py
```

Then in Open WebUI add an OpenAPI tool server pointing at the proxy
(`http://localhost:9000`). Browse `http://localhost:9000/docs` to verify the tools.

> **Port note:** the proxy and the server need different ports, and Open WebUI's
> own stack may already occupy `8000` — pick free ports (e.g. `9000` for the
> proxy) if you hit "address already in use".

For personal use, run it locally (stdio, or HTTP bound to `127.0.0.1`). To share
it more widely you have two sanctioned paths: package it as an **MCPB** bundle
(ships its own runtime, no Python setup for others), or host the HTTP transport
behind a reverse proxy that adds TLS and authentication.

## Development

`uv sync` installs the dev tooling (pytest, respx, ruff) from the `dev` dependency
group. The test suite is fully offline — `respx` mocks the BookLore API, so no live
instance is needed.

```bash
uv run pytest               # run the test suite
uv run ruff check .         # lint
uv run ruff format .        # format
uv run mypy                 # type check (server.py, log.py)
```

CI (`.github/workflows/ci.yml`) runs lint, format check, type check, and tests
across Python 3.10–3.13 on every push and pull request, plus a dependency
vulnerability scan (`pip-audit`) and a Docker image build.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev workflow, how to add a tool, and
testing conventions.

## License

[MIT](LICENSE) © Willian Paixão
