# syntax=docker/dockerfile:1

FROM node:24-alpine AS frontend
WORKDIR /src
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.13-slim AS app
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    COVERARR_CONFIG_DIR=/config

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/coverarr ./coverarr
COPY backend/migrations ./migrations
RUN uv sync --frozen --no-dev
COPY --from=frontend /src/dist ./coverarr/static

RUN useradd --uid 1000 --user-group --no-create-home coverarr \
    && mkdir -p /config && chown coverarr:coverarr /config
USER coverarr
VOLUME ["/config"]
EXPOSE 2683
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:2683/api/health')" || exit 1
CMD ["uvicorn", "coverarr.main:app", "--host", "0.0.0.0", "--port", "2683", "--proxy-headers", "--forwarded-allow-ips", "*"]
