# The container image is the single artifact (§4.1) — no PyInstaller/Nuitka.
#
# Build from the repository root (so src/, pyproject.toml, uv.lock and .dockerignore
# apply):
#
#   docker build -f deploy/Dockerfile -t youtube-mcp:dev .
#
# `README.md` is in the COPY list because pyproject.toml declares it as the project
# readme, and the hatchling metadata build fails without it.

# --- Stage 1: builder -------------------------------------------------------------------
# Same minor as `.python-version`, so uv resolves the base interpreter rather than
# downloading one. uv itself never reaches the runtime stage.
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.23 /uv /uvx /bin/

# `copy` keeps the venv relocatable (no hardlinks into the uv cache, which is not copied).
ENV UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock .python-version README.md ./
COPY src ./src

# `--frozen`: uv.lock is the source of truth — fail the build instead of re-resolving.
# `--no-dev`: no pytest/coverage in the image.
# `--compile-bytecode`: pre-warm .pyc so container start does not pay compile cost.
RUN uv venv /app/.venv \
 && uv sync --frozen --no-dev --compile-bytecode

# --- Stage 2: runtime -------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# Non-root by default. The ids are build args so the cluster can pin them if it needs to.
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --gid "${APP_GID}" app \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --shell /usr/sbin/nologin app

# Image-level defaults only; everything here is overridable per deployment (§12).
# DATABASE_PATH is absolute because the CWD-relative default (`cache.db`) would land in
# /app and vanish on restart.
# MCP_TRANSPORT is deliberately NOT set: the image keeps the code default (stdio), so it is a
# drop-in local-agent server and `docker run youtube-mcp` behaves like `uv run youtube-mcp`.
# The transport follows MCP_TRANSPORT when the deployment sets it. Two run shapes:
#   stdio (default): docker run -i --rm -e YOUTUBE_API_KEY=... youtube-mcp:dev
#     `-i` is required: the agent drives stdin/stdout. With YOUTUBE_API_KEY set and no piped
#     stdin the server reads EOF and exits 0 immediately — correct MCP stdio behaviour, not a
#     crash (a keyless run fails fast, exit 1, before EOF matters).
#   http (override): docker run --rm -p 8088:8088 -e MCP_TRANSPORT=http -e YOUTUBE_API_KEY=... youtube-mcp:dev
#     long-running service; GET /health answers and `docker stop` is clean.
ENV VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/data/cache.db

WORKDIR /app

# Only the venv and the package source — no uv, no build tooling, no tests.
# src/ is copied to /app/src because that is the path the editable install recorded inside
# the venv; keep the two in step if either moves.
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

# Cache directory, existing and writable by the non-root user. A PVC (or emptyDir) can be
# mounted here without a permissions surprise (§14, §18.1): a cold cache costs quota, not
# correctness.
RUN install -d -o app -g app -m 0750 /data

USER app

EXPOSE 8088

# The console script, not uvicorn directly: `main()` dispatches on MCP_TRANSPORT (stdio ->
# `mcp.run()`, http -> uvicorn with the `create_app` factory). `sh -c` + `exec` keeps the
# server as PID 1, so MCP_HOST / MCP_PORT and `docker stop` both reach it.
ENTRYPOINT ["/bin/sh", "-c", "exec youtube-mcp"]
