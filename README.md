# BookOrbit MCP server

A local [MCP](https://modelcontextprotocol.io) server that wraps a self-hosted
[**BookOrbit**](https://github.com/bookorbit/bookorbit) instance's REST API so Claude
can search your library, read and edit metadata, manage collections, follow series,
and look at your reading history and statistics.

Runs as a long-lived **HTTP** server (default) or over **stdio** (for local Claude
launches), and talks to your BookOrbit over HTTP. Single-user: it authenticates with a
BookOrbit [API key](#configure-the-api-key).

**Supported BookOrbit version:** v3.8.0.

## Tools

Every listing tool filters, sorts and pages on the server, so the server never loads
your whole library, however large it is. The **Key** column says what the API key
needs (see [Configure the API key](#configure-the-api-key)): *read* is a Read key with
no extra permissions; *write* is a Read-and-write key, plus the named permission.

### Find and read books

| Tool | Key | What it does |
|---|---|---|
| `search_books` | read | Search and filter (free text, tags, genres, authors, series, collections, libraries, read status, **missing fields**, raw BookOrbit rules), sorted and paginated |
| `list_books` | read | List books or run a quick free-text search |
| `get_book` | read | Full record for one book: metadata, files, collections, provider ids, ratings, locks |
| `books_by_author` | read | Books by an author (name substring) |
| `list_authors` | read | Authors with per-author book counts |
| `get_recommendations` | read | Related books: similar, same series, or same author |
| `list_collections` / `get_collection_books` | read | Your collections (and public ones) and the books in one |
| `list_series` / `get_series_books` | read | Series with your progress, missing volumes and next book; books in reading order |
| `list_smart_scopes` / `get_smart_scope_books` | read | Saved searches and the books they currently match |
| `list_libraries` | read | Libraries with book counts, folders on the server and allowed formats |
| `export_library` | read | Export books (all, a collection, a library, or a search) as JSON or CSV |
| `ping` | read | Is the server up, is the key accepted, which permissions it has, server version |

### Your reading

| Tool | Key | What it does |
|---|---|---|
| `get_reading_stats` | read | Book counts by read status, your rating distribution and average |
| `get_reading_statistics` | read | Reading reports: daily reading, heatmap, peak hours, pace, completions, time per genre, … |
| `get_reading_history` | read | Your read-throughs with dates and outcomes, period summaries, a book's session stats |
| `get_annotations` | read | Your highlights, notes and bookmarks in a book |
| `set_read_status` / `bulk_set_read_status` | write | Set reading status (reading, read, abandoned, …), with optional start/finish dates |
| `set_reading_progress` | write | Record your position in a book file |
| `reset_progress` | write | Clear reading positions, or the whole reading state (destructive) |

### Library statistics

| Tool | Key | What it does |
|---|---|---|
| `get_library_statistics` | read | Totals, genre/language/format distributions, top authors and series, metadata completeness and freshness, growth over time, … |

### Metadata

| Tool | Key | What it does |
|---|---|---|
| `isbn_lookup` | read | Look up an ISBN with the server's metadata providers |
| `fetch_metadata_candidates` | read | Candidate metadata for a book from the providers (review, then apply) |
| `update_book_metadata` | write + `library_edit_metadata` | Edit a book's metadata: only the fields you pass; `clear_fields` empties fields |
| `add_tags` / `remove_tags` | write + `library_edit_metadata` | Add/remove tags on books (merge; skips books with tags locked) |
| `add_genres` / `remove_genres` | write + `library_edit_metadata` | Add/remove genres on books (merge; skips books with genres locked) |
| `bulk_update_metadata` | write + `library_edit_metadata` | Patch many books at once (tags, genres, any field, clears), using BookOrbit's bulk edit where it can |
| `set_field_locks` | write + `library_edit_metadata` | Lock/unlock fields (or `all`) so curated values survive refreshes |
| `set_personal_rating` | write + `library_edit_metadata` | Set or clear your 1-5 rating |
| `refresh_metadata` | write + `library_edit_metadata` | Re-fetch metadata from providers: preview by default, apply with `preview=False` |

### Collections and library maintenance

| Tool | Key | What it does |
|---|---|---|
| `create_collection` / `delete_collection` | write | Create a collection; delete one (its books are kept) |
| `add_to_collections` / `remove_from_collections` | write | Collection membership (other collections untouched) |
| `refresh_library` | write + `manage_libraries` | Scan a library's folders to import files added on disk; optionally wait for the result |
| `book_dock_rescan` | write + `manage_book_dock` | Rescan the book dock (drop folder) for new files awaiting review |
| `find_duplicates` | write + `library_delete_books` | Run (or reuse) BookOrbit's duplicate scan and list the groups; merges and deletes nothing |

### Working with the tools

**Finding books to enrich.** `search_books`' `missing` filter runs on the server and
covers fields like `isbn`, `description`, `genres`, `cover` and per-provider community
ratings such as `amazonRating`; `missing_mode: "any"` keeps books lacking at least one
of them. Collection and library filters take ids from `list_collections` and
`list_libraries`.

**Editing.** `add_tags` / `add_genres` and `bulk_update_metadata` merge and touch only
the fields they manage. `update_book_metadata` writes only the fields you include; list
fields you pass (`authors`, `genres`, `tags`) replace the whole list, and `clear_fields`
empties a field without touching the others. BookOrbit refuses to write a locked field,
so lock curated fields with `set_field_locks` and unlock them before editing. On a
library with file writing enabled, BookOrbit also writes metadata changes into the book
files.

**Metadata lookups.** `fetch_metadata_candidates` and `isbn_lookup` use the metadata
providers enabled on your server (Google Books, Goodreads, Amazon, OpenLibrary, …). With
only a `book_id`, `fetch_metadata_candidates` searches with the book's own stored
metadata. Both report each provider as `ok`, `empty` (ran, no match), `timeout` /
`throttled` / `failed` (the provider broke, so its silence is not a real no-match) or
`disabled` (not enabled on the server). Each candidate carries only its own provider's
fields, so include the provider that owns the field you want to fill.
`refresh_metadata` previews by default; check the preview before applying.

**Long-running jobs.** `refresh_library` and `find_duplicates` start background jobs on
the server. Both can wait for the result; `find_duplicates` returns a `scan_id` to call
back with if the scan outlasts `wait_seconds`.

## Setup

Uses [uv](https://docs.astral.sh/uv/). `uv sync` creates the virtualenv and installs
dependencies; `uv run` auto-syncs before running, so you never have to activate
anything.

```bash
cd bookorbit-mcp
uv sync                   # creates .venv, installs fastmcp + httpx
cp .env.example .env      # then edit with your BookOrbit URL + API key
```

## Configure the API key

The server authenticates with a BookOrbit API key, sent as `X-Api-Key` on every
request.

1. In BookOrbit, open **Settings > You > API keys**. Creating keys needs the
   **Create API keys** permission, which an administrator can grant.
2. Create a key for this server:
   - **Access:** **Read** is enough for the read tools. The write tools need
     **Read and write**.
   - **Permissions:** grant only what the tools you use need (see the **Key** column in
     [Tools](#tools)). A key with no permissions can still read your libraries.
   - **Libraries:** optionally limit the key to the libraries it should touch.
   - **Expires:** prefer an expiry, and regenerate the key on a schedule.
3. Copy the key (it is shown once and starts with `bok_`) into `BOOKORBIT_API_KEY`.

Keep the key in `.env` or a secret store, never in source control. The `ping` tool
reports whether the key works and which permissions it actually has. When a tool needs
something the key lacks, the error says which permission is missing, or that the key is
read-only.

The server reads these environment variables (see `.env.example`):

- `BOOKORBIT_URL` — your BookOrbit base URL (default `http://localhost:3000`)
- `BOOKORBIT_API_KEY` — your BookOrbit API key (required)
- `BOOKORBIT_TIMEOUT` — per-request timeout in seconds (default `120`). Raise this if
  you hit timeouts on large operations.
- `BOOKORBIT_RETRIES` — retries for transient failures (timeouts, 429/5xx) with
  exponential backoff (default `2`); `BOOKORBIT_BACKOFF` sets the base delay in
  seconds (default `0.5`).

## Logging

Logging is handled by [structlog](https://www.structlog.org/) and configured with two
environment variables:

- `LOG_LEVEL` — `DEBUG`, `INFO` (default), `WARNING`, `ERROR`, or `CRITICAL`
- `LOG_FORMAT` — `console` (default; human-readable, coloured on a TTY) or `json`
  (one JSON object per line, for log aggregators)

Logs are written to **stderr** so they never interfere with the MCP JSON-RPC stream on
stdout. Both this server's logs and those of its libraries (httpx, uvicorn, …) share
the same format. The Docker image defaults to `LOG_FORMAT=json`.

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

Serves streamable-HTTP MCP at `http://127.0.0.1:8000/mcp`. Override with `MCP_HOST`,
`MCP_PORT`, `MCP_PATH`. Press Ctrl-C to stop. `uv run bookorbit-mcp` (the installed
console script) is equivalent to `uv run server.py`.

> **Security:** this endpoint has no auth of its own and acts with your API key, so
> anyone who can reach the port can do whatever the key allows. Keep it bound to
> `127.0.0.1` (the default). Only set `MCP_HOST=0.0.0.0` behind a reverse proxy that
> adds TLS + authentication.

### stdio (local process)

```bash
set -a && source .env && set +a
MCP_TRANSPORT=stdio uv run server.py
```

Speaks MCP over stdin/stdout, so on its own it just blocks waiting for a client — this
is the form an MCP host launches directly (see Claude Code/Desktop below).

## Run with Docker

The image runs the HTTP transport bound to `0.0.0.0` inside the container; you control
exposure with the published port. The API key is passed in at runtime, so it's never
baked into the image.

```bash
docker build -t bookorbit-mcp .
docker run --rm --env-file .env -p 127.0.0.1:8000:8000 bookorbit-mcp
```

`--env-file .env` loads your API key from the `.env` file (same one from
[Configure the API key](#configure-the-api-key)) — each `KEY=value` line becomes an
environment variable in the container:

```bash
# .env
BOOKORBIT_URL=http://host.docker.internal:3000
BOOKORBIT_API_KEY=bok_...
```

The path is relative to where you run the command (`--env-file ./path/to/.env` for
another location). To pass variables individually instead of a file, use `-e`:

```bash
docker run --rm \
  -e BOOKORBIT_URL=http://host.docker.internal:3000 \
  -e BOOKORBIT_API_KEY=bok_... \
  -p 127.0.0.1:8000:8000 bookorbit-mcp
```

The MCP endpoint is served at `http://127.0.0.1:8000/mcp`.

> **Security:** the publish mapping above binds to `127.0.0.1` on purpose — the endpoint
> has no auth of its own and acts with your API key. If your BookOrbit runs in its own
> container, point `BOOKORBIT_URL` at it over a shared Docker network (or
> `host.docker.internal`) rather than `localhost`. Only expose the port beyond loopback
> behind a reverse proxy that adds TLS + authentication.

## Test it

Everything here is pure Python — no Node/npm required.

List the tools and their schemas:

```bash
uv run fastmcp list server.py
```

Check that the server reaches BookOrbit and the key works, against a running HTTP
server (see [HTTP](#http-long-lived-server-default)):

```bash
uv run fastmcp call http://127.0.0.1:8000/mcp ping
uv run fastmcp call http://127.0.0.1:8000/mcp list_books query=tolkien
```

Arguments are `key=value` pairs; use `--input-json '{...}'` for nested ones.

## Add to Claude Code

**HTTP (default)** — start the server (see [HTTP](#http-long-lived-server-default)),
then connect to it:

```bash
claude mcp add --transport http bookorbit http://127.0.0.1:8000/mcp
```

**Local (stdio)** — Claude launches the process itself (note `MCP_TRANSPORT=stdio`):

```bash
claude mcp add bookorbit \
  --scope user \
  --env MCP_TRANSPORT=stdio \
  --env BOOKORBIT_URL=http://localhost:3000 \
  --env BOOKORBIT_API_KEY=bok_... \
  -- uv --directory /absolute/path/to/bookorbit-mcp run server.py
```

`uv --directory <path> run` resolves the project's environment automatically, so
`fastmcp` and `httpx` are always available.

## Add to Claude Desktop

Claude Desktop launches the process, which must speak stdio — so pin
`MCP_TRANSPORT=stdio`. Edit `claude_desktop_config.json` (Settings → Developer → Edit
Config):

```json
{
  "mcpServers": {
    "bookorbit": {
      "command": "uv",
      "args": ["--directory", "/abs/path/to/bookorbit-mcp", "run", "server.py"],
      "env": {
        "MCP_TRANSPORT": "stdio",
        "BOOKORBIT_URL": "http://localhost:3000",
        "BOOKORBIT_API_KEY": "bok_..."
      }
    }
  }
}
```

## Use with Open WebUI (OpenAPI)

Open WebUI consumes **OpenAPI tool servers**, not MCP directly. Bridge to it with
[`mcpo`](https://github.com/open-webui/mcpo), the MCP→OpenAPI proxy: it wraps this
server and exposes every tool as a REST endpoint plus an auto-generated `/openapi.json`
and `/docs`.

With the server already running over HTTP (the default):

```bash
# server on :8000/mcp, proxy serving OpenAPI on :9000
uvx mcpo --port 9000 --server-type streamable-http -- http://127.0.0.1:8000/mcp
```

Or let `mcpo` launch and manage the server itself over stdio (no separate process to
run — pass the BookOrbit env through with `-e`):

```bash
uvx mcpo --port 9000 \
  -e MCP_TRANSPORT=stdio \
  -e BOOKORBIT_URL=http://localhost:3000 \
  -e BOOKORBIT_API_KEY=bok_... \
  -- uv --directory /abs/path/to/bookorbit-mcp run server.py
```

Then in Open WebUI add an OpenAPI tool server pointing at the proxy
(`http://localhost:9000`). Browse `http://localhost:9000/docs` to verify the tools.

> **Port note:** the proxy and the server need different ports, and Open WebUI's own
> stack may already occupy `8000` — pick free ports (e.g. `9000` for the proxy) if you
> hit "address already in use".

For personal use, run it locally (stdio, or HTTP bound to `127.0.0.1`). To share it
more widely you have two sanctioned paths: package it as an **MCPB** bundle (ships its
own runtime, no Python setup for others), or host the HTTP transport behind a reverse
proxy that adds TLS and authentication.

## Development

`uv sync` installs the dev tooling (pytest, respx, ruff, mypy) from the `dev`
dependency group. The test suite is offline by default — `respx` mocks the BookOrbit
API — and opt-in live tests run read-only against a real server.

```bash
uv run pytest               # run the test suite
uv run ruff check .         # lint
uv run ruff format .        # format
uv run mypy                 # type check (server.py, log.py)
```

CI (`.github/workflows/ci.yml`, and `.gitea/workflows/ci.yml` for Gitea) runs lint,
format check, type check, and tests across Python 3.12–3.14 on every push and pull
request, plus a dependency vulnerability scan (`pip-audit`); the GitHub workflow also
builds the Docker image.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev workflow, how to add a tool, and
testing conventions.

## License

[MIT](LICENSE) © Willian Paixão
