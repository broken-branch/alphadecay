FROM node:24.16.0-alpine@sha256:bc23e6976e92708e9eadae437d7dd180b3fd47ed75edf322d6cfa36eba4a7fc8 AS frontend

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY index.html tsconfig.json vite.config.mts vitest.config.ts ./
COPY frontend ./frontend
COPY public ./public
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.12.3@sha256:dfd1e6972e100ca2fbf1f391effc3dd4aa57f319bf03c3e321e0a3f3341ed5af AS uv

FROM python:3.12.13-slim-bookworm@sha256:6e13e65c55e33adf203d77ee371cf8bf5d81bd4902ef07565721f46bf44917af AS application

ENV PATH="/app/.venv/bin:$PATH" \
    APP_RUNTIME_CONFIG_REQUIRED=false \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
COPY backend ./backend
RUN uv sync --frozen --no-dev --no-editable

COPY third_party ./third_party
COPY fixtures ./fixtures
COPY migrations ./migrations
COPY --from=frontend /app/dist ./dist

RUN groupadd --system alphadecay \
    && useradd --system --gid alphadecay --home-dir /app alphadecay \
    && chown -R alphadecay:alphadecay /app

USER alphadecay
EXPOSE 10000

CMD ["sh", "-c", "exec uvicorn backend.app.main:app --host 0.0.0.0 --port ${PORT:-10000}"]
