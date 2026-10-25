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
| sqlalchemy | not yet added; pinned when first added |
| alembic | not yet added; pinned when first added |
| pgvector | not yet added; pinned when first added |
| opentelemetry-sdk | not yet added; pinned when first added |
| boto3 | not yet added; pinned when first added |

Development tools, as locked in `uv.lock` on the date above:

| Package | Pinned version |
|---|---|
| mypy | 2.4.0 |
| pytest | 9.1.1 |
| ruff | 0.17.2 |

## Consequences

Any checkout builds the same environment with `uv sync`. Pinned versions can fall behind security fixes, so upgrades need their own reviewed PR rather than riding along with feature work. This table must be updated when a package is first added.

## Alternatives

- Version ranges in `pyproject.toml` with the lock file used only in CI. Rejected: developer machines and CI would drift apart.
- Pin only at release time. Rejected: early development has the most churn, which is when pins matter most.
