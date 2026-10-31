# ADR-0002: Pin dependencies

- Status: Accepted
- Date: 2026-10-25

## Context

The agent's behavior depends on the exact versions of its SDKs, the Temporal server and client, and the database drivers. Floating versions make a run from last week hard to reproduce, and a dependency update can change agent behavior without any code change in this repo.

## Decision

Pin the latest stable versions at project start with `uv lock` and commit `uv.lock`. The key versions are recorded below so a reader can see them without opening the lock file. Upgrades go in their own `chore/` PR.

Key packages, as locked in `uv.lock` on 2026-10-30:

| Package | Pinned version |
|---|---|
| temporalio | 1.34.0 (runtime since LH-203, see ADR-0004) |
| anthropic | 2.3.0 (`anthropic>=2.3,<3`, see ADR-0003) |
| openai | 3.41.0 |
| httpx2 | 2.16.0 |
| httpcore2 | 2.16.0 |
| docker | 7.2.0 |
| fastapi | not yet added; pinned when first added |
| jsonschema | 4.26.0 |
| typer | 0.27.3 (`typer>=0.27.3`, for the `longhaul` command; it bundles its own Click, so `click` is not installed) |
| pydantic | 2.14.1 |
| pydantic-settings | 2.16.1 |
| pyyaml | 6.0.3 |
| sqlalchemy | 2.1.6 |
| alembic | 1.20.1 |
| asyncpg | 0.32.0 |
| pgvector | 0.5.1 |
| psycopg | 3.3.6 |
| opentelemetry-sdk | not yet added; pinned when first added |
| boto3 | 1.43.118 (runtime since LH-202, see ADR-0005) |

`psycopg` sits in the `dev` dependency group of `pyproject.toml` today. `boto3` moved to the runtime dependencies with LH-202, and its type stubs sit in the `dev` group. `temporalio` moved to the runtime dependencies with LH-203, because the workflow and the client in `longhaul.durable` import it, and a worker image built without the `dev` group needs it. `httpx2` and `httpcore2` are not declared by us: `openai` pulls them in, and `anthropic` 2.3.0 requires `httpx2>=2.0.0,<3` as well. The old `httpx` package is not installed, so test doubles for either SDK use `httpx2.MockTransport`. `anthropic` 2.3.0 sends API version `2026-10-21` unless told otherwise. A test in `tests/unit/test_anthropic_provider.py` fails when an upgrade moves that version, so the change is read before it merges.

Development tools, as locked in `uv.lock` on 2026-10-30:

| Package | Pinned version |
|---|---|
| mypy | 2.4.0 |
| pytest | 9.1.1 |
| pytest-asyncio | 1.4.1 |
| pytest-cov | 7.1.0 |
| ruff | 0.17.2 |
| pre-commit | 4.7.0 |
| types-boto3 | 1.43.123 |
| types-docker | 7.2.0.20261006 |
| types-jsonschema | 4.26.0.20261006 |
| types-pyyaml | 6.0.12.20260906 |

### Container images (docker-compose.yml)

| Service | Image |
|---|---|
| postgres | pgvector/pgvector:0.8.0-pg17 |
| temporal | temporalio/temporal:1.5.1 (`server start-dev`, SQLite file on a named volume) |
| minio | bitnamilegacy/minio:2025.7.23-debian-12-r3 |
| minio-init | bitnamilegacy/minio-client:2025.7.21-debian-12-r2 |
| jaeger | jaegertracing/all-in-one:1.69.0 |
| otel-collector | otel/opentelemetry-collector-contrib:0.123.0 |
| prometheus | prom/prometheus:v3.3.0 |
| grafana | grafana/grafana:11.6.0 |

MinIO no longer publishes `minio/minio` or `minio/mc` images, so we use the `bitnamilegacy` builds. That build has no web console, so only the S3 port (9000) is exposed. The Temporal dev server runs as root inside its container because the named volume is root-owned; this is a local dev service only.

## Consequences

Any checkout builds the same environment with `uv sync`. Pinned versions can fall behind security fixes, so upgrades need their own reviewed PR rather than riding along with feature work. This table must be updated when a package is first added.

## Alternatives

- Version ranges in `pyproject.toml` with the lock file used only in CI. Rejected: developer machines and CI would drift apart.
- Pin only at release time. Rejected: early development has the most churn, which is when pins matter most.
