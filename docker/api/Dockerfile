# The console API with the built console: one process that answers /api and serves the pages.
# Build it from the repo root, as docker-compose.yml does:
#   docker build -f docker/api/Dockerfile -t longhaul-api .
#
# The first stage builds console/ with Node. The second is the Python API, which serves the
# result from LONGHAUL_CONSOLE_DIR (src/longhaul/api/console.py). Only the second stage ships.

FROM node:26-slim AS console
WORKDIR /console
# The packages first, so a change to the source does not install them again.
COPY console/package.json console/package-lock.json ./
RUN npm ci
COPY console/ ./
RUN npm run build

# Same base as the worker image, pinned to a patch release for the same reason.
FROM python:3.12.15-slim-trixie

RUN pip install --no-cache-dir uv==0.12.3

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# No memory extra: the API lists memory items and decides on them, and neither embeds a text
# (src/longhaul/api/deps.py), so the image does not carry fastembed or its model.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-group eval --no-install-project

COPY src ./src
COPY config ./config
RUN uv sync --locked --no-dev --no-group eval --no-editable

COPY --from=console /console/dist /app/console
ENV LONGHAUL_CONSOLE_DIR=/app/console
ENV LONGHAUL_CONFIG=/app/config/longhaul.example.yaml

# Inside the container the server must listen on every interface, or the published port would
# reach nothing. docker-compose.yml publishes it on 127.0.0.1 only: the console has no login
# until LH-807.
ENV LONGHAUL_API_HOST=0.0.0.0
EXPOSE 8000

CMD ["longhaul", "api"]
