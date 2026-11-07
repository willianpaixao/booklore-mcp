# syntax=docker/dockerfile:1
# Pinned base image: an exact uv release with Python 3.13 on Debian 13 (trixie) slim, plus
# its multi-arch (amd64/arm64) index digest, so every build starts from identical layers.
# Bump the tag and the digest together. Don't go back to a floating tag:
# `python3.13-bookworm-slim` stopped moving at uv 0.9.30 when uv dropped bookworm images.
FROM ghcr.io/astral-sh/uv:0.13.6-python3.13-trixie-slim@sha256:e586c1c65b7071aec78f02c5587c76c5011159cd76b3d6f1255f40e48e850abc

# uv tuning: copy deps into the image (no symlinks to the cache) and don't try to
# manage the Python install — the base image already provides 3.13.
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
COPY src ./src
COPY server.py README.md LICENSE ./
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
