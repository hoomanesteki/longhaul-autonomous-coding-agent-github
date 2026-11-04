# The worker: the process that polls Temporal and runs the activities of every task.
# Build it from the repo root, as docker-compose.yml does:
#   docker build -f docker/worker/Dockerfile -t longhaul-worker .
#
# The worker starts sandbox containers, so it needs the Docker socket and the checkpoint folder
# mounted at run time. See "The worker in Docker" in docs/runbook.md, including the security
# note about the socket.

# Same base as the sandbox image, pinned to a patch release for the same reason.
FROM python:3.12.15-slim-trixie

# The checkpoint store runs git on the host side of each task's bare repo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# The sandbox user writes into the bare repos through a mount, so objects in them can belong to
# another uid than the worker's. Git refuses such a repo unless it is trusted.
RUN git config --system --add safe.directory '*'

RUN pip install --no-cache-dir uv==0.12.3

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}"

WORKDIR /app

# The dependencies first, so a change to the source does not install them again.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
COPY config ./config
RUN uv sync --locked --no-dev --no-editable

# The settings come from LONGHAUL_* variables (see .env.example). The config file is the example
# one unless LONGHAUL_CONFIG names another.
ENV LONGHAUL_CONFIG=/app/config/longhaul.example.yaml

# SIGTERM from `docker stop` starts a graceful stop (src/longhaul/durable/worker.py).
CMD ["longhaul", "worker"]
