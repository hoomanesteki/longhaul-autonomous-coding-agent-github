# Base image for every task sandbox. Pinned to a patch release so a rebuild
# does not silently pick up a different Debian or Python.
FROM python:3.12.15-slim-trixie

# Only the tools the agent needs. Every extra package is attack surface and
# image size, so keep this list short.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ripgrep patch procps \
    && rm -rf /var/lib/apt/lists/*

# Fixed uid/gid so bind-mounted volumes can be owned by the same id on the host.
RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --home-dir /home/agent --create-home --shell /bin/bash agent

# /workspace and /ckpt are mounted volumes. Their owner is not known at build
# time, and git refuses repositories owned by another uid unless this allows it.
RUN git config --system user.name "longhaul" \
    && git config --system user.email "longhaul@localhost" \
    && git config --system --add safe.directory '*'

WORKDIR /workspace
RUN chown agent:agent /workspace

ENV PYTHONUNBUFFERED=1 \
    LANG=C.UTF-8

USER agent

ENTRYPOINT ["sleep", "infinity"]
