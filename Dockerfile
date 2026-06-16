# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# uv tuning: copy deps into the image (no symlinks to the cache) and don't try to
# manage the Python install — the base image already provides 3.12.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install dependencies first (cached layer) from the lockfile, without the project
# or dev/test dependencies.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Then the application code, and install the project itself.
COPY server.py log.py README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# Run as an unprivileged user.
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

ENV PATH="/app/.venv/bin:$PATH"

# Inside a container the loopback default is useless — bind to all interfaces and
# let the container's published port (and any reverse proxy) control exposure.
ENV MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp \
    BOOKLORE_URL=http://localhost:6060 \
    LOG_LEVEL=INFO \
    LOG_FORMAT=json

EXPOSE 8000

# Liveness: confirm the MCP server is accepting connections on its port.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ.get('MCP_PORT','8000'))), timeout=3)" || exit 1

# Credentials are supplied at runtime (docker run --env-file .env).
CMD ["booklore-mcp"]
