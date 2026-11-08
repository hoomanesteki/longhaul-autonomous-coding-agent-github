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

# The example config turns memory on with the local embedder, which needs fastembed (the memory
# extra, ADR-0010). A worker without it stops at its start. Every sync names the extra, because a
# sync without it removes the packages that an earlier one installed.
#
# The dependencies first, so a change to the source does not install them again.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-group eval --no-install-project --extra memory

# The model is part of the image: a worker then starts with no network, and every container of
# a build embeds with the same file, which the hub could otherwise replace between two builds.
# fastembed would use the system temp directory, which is not a place to keep it. The model id
# is the one in src/longhaul/memory/embedder.py (tests/unit/test_worker_image.py compares them).
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('BAAI/bge-small-en-v1.5')"

COPY src ./src
COPY config ./config
COPY templates ./templates
RUN uv sync --locked --no-dev --no-group eval --no-editable --extra memory

# The settings come from LONGHAUL_* variables (see .env.example). The config file is the example
# one unless LONGHAUL_CONFIG names another.
ENV LONGHAUL_CONFIG=/app/config/longhaul.example.yaml
# The pull request body is a template. The installed package is not inside a checkout, so the
# worker is told where the folder is.
ENV LONGHAUL_TEMPLATES_DIR=/app/templates

# SIGTERM from `docker stop` starts a graceful stop (src/longhaul/durable/worker.py).
CMD ["longhaul", "worker"]
