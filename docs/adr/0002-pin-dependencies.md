# ADR-0002: Pin dependencies

- Status: Accepted
- Date: 2026-10-25

## Context

The agent's behavior depends on the exact versions of its SDKs, the Temporal server and client, and the database drivers. Floating versions make a run from last week hard to reproduce, and a dependency update can change agent behavior without any code change in this repo.

## Decision

Pin the latest stable versions at project start with `uv lock` and commit `uv.lock`. The key runtime versions are recorded below so a reader can see them without opening the lock file. Upgrades go in their own `chore/` PR.

Key runtime packages:

| Package | Pinned version |
|---|---|
| temporalio | not yet added; pinned when first added |
| anthropic | not yet added; pinned when first added |
| docker | not yet added; pinned when first added |
| fastapi | not yet added; pinned when first added |
| sqlalchemy | 2.1.6 |
| alembic | 1.20.1 |
| asyncpg | 0.32.0 |
| pgvector | 0.5.1 |
| opentelemetry-sdk | not yet added; pinned when first added |
| boto3 | not yet added; pinned when first added |

Development tools, as locked in `uv.lock` on the date above:

| Package | Pinned version |
|---|---|
| mypy | 2.4.0 |
| pytest | 9.1.1 |
| ruff | 0.17.2 |

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
