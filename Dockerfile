# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.19@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6 AS uv

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Cache third-party dependencies separately from the application sources.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE NOTICE ./
COPY src ./src
COPY contracts ./contracts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337 AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app \
    && mkdir /data \
    && chown 10001:10001 /data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY config.replay.yaml ./config.replay.yaml
COPY examples/replay ./examples/replay
USER 10001:10001
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["macro-event-alerts", "health", "--state", "/data/state.db"]
ENTRYPOINT ["macro-event-alerts"]
CMD ["replay", "--config", "/app/config.replay.yaml", "--fixture", "/app/examples/replay/demo.jsonl"]
