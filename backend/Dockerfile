# One image, several entrypoints: API (default CMD), worker (`docintel worker`), migrations
# (`alembic upgrade head`) and the management CLI (`docintel ...`); MCP server in Phase 7.

ARG PYTHON_VERSION=3.13

# ---------------------------------------------------------------------------- build
FROM python:${PYTHON_VERSION}-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install "uv==0.11.32"

WORKDIR /app
# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock ./
# --no-default-groups: runtime dependencies only (no dev tools, no synthetic-data generator).
RUN uv sync --frozen --no-default-groups --no-install-project
COPY src ./src
RUN uv sync --frozen --no-default-groups --no-editable

# ---------------------------------------------------------------------------- runtime
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    LOG_FORMAT=json

RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data/storage \
    && chown app:app /data/storage

# Local document storage (mounted as a volume shared by api and worker).
ENV STORAGE_LOCAL_ROOT=/data/storage

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini ./

USER app
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"]

# --proxy-headers: client IPs come from X-Forwarded-For set by nginx; which proxies are trusted
# is controlled by FORWARDED_ALLOW_IPS (set in docker-compose.yml).
CMD ["uvicorn", "docintel.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--no-server-header"]
