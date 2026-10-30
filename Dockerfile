# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.11.19@sha256:b46b03ddfcfbf8f547af7e9eaefdf8a39c8cebcba7c98858d3162bd28cf536f6 AS uv

FROM python:3.12-alpine3.23@sha256:a4ad86fc2eaa4f9a5c3153776c9fb56e502dbd8e23dfa855d164308f376a6dd2 AS builder
COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Only dependency changes invalidate the binary wheel installation.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --no-build
COPY README.md LICENSE NOTICE ./
COPY src ./src
COPY contracts ./contracts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-alpine3.23@sha256:a4ad86fc2eaa4f9a5c3153776c9fb56e502dbd8e23dfa855d164308f376a6dd2 AS runtime
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN addgroup -S -g 10001 app \
    && adduser -S -D -H -u 10001 -G app app \
    && mkdir /data \
    && chown 10001:10001 /data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY config.replay.yaml config.live.yaml ./
COPY examples/replay ./examples/replay
USER 10001:10001
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD ["macro-event-alerts", "health", "--state", "/data/state.db"]
ENTRYPOINT ["macro-event-alerts"]
CMD ["run", "--config", "/app/config.live.yaml"]
