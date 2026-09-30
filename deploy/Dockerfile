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

COPY --from=ghcr.io/astral-sh/uv:0.12.11 /uv /uvx /bin/

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

# `--factory` because `create_app()` returns the app (§13). `sh -c` + `exec` so MCP_HOST /
# MCP_PORT actually take effect and signals still reach uvicorn; the console script
# (`youtube-mcp`) is the equivalent path for stdio and local use.
ENTRYPOINT ["/bin/sh", "-c", "exec uvicorn youtube_mcp.server:create_app --factory --host \"${MCP_HOST:-0.0.0.0}\" --port \"${MCP_PORT:-8088}\""]
