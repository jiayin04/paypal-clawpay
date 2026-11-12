FROM python:3.12-slim AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev

FROM python:3.12-slim
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY --from=dependencies /app/.venv /app/.venv
COPY app ./app
USER 10001:10001
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "18000", "--workers", "1", "--no-access-log"]
