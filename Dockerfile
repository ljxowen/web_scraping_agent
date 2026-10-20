# Deployment path: one container image runs the crawler as a batch job (cron / k8s CronJob / ECS task).
# Base image tag tracks the locked playwright version (uv.lock).
FROM mcr.microsoft.com/playwright/python:v1.64.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 UV_PROJECT_ENVIRONMENT=/opt/venv
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config.yaml ./
RUN uv sync --frozen --no-dev && uv run playwright install chromium

# Secrets (MINIMAX_API_KEY) are injected at runtime from the orchestrator's secret store, never baked in.
# Mount /app/data (SQLite / checkpoint) and /app/output on a persistent volume.
VOLUME ["/app/data", "/app/output", "/app/logs"]
ENTRYPOINT ["uv", "run", "--no-sync", "safco-agent"]
CMD ["crawl", "--full"]
