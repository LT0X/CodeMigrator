FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.7 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY descriptors ./descriptors
COPY migrations ./migrations

RUN uv sync --frozen --no-dev

CMD ["uv", "run", "--no-dev", "codemigrator-app"]
