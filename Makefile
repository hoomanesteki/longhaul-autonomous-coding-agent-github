.PHONY: setup check ci-local itest e2e fake-github up down migrate sandbox-image api-types console-check console-e2e site site-preview gate

PHASE ?= 0

# Tests that only run on request: paid live calls, slow tests and the full chaos suite.
# Gates, `make itest`, `make e2e` and the CI integration job leave them out. chaos_lite is
# the short chaos run that gates and CI keep.
OPT_IN := not live and not slow and not (chaos and not chaos_lite)

setup:
	uv sync
	uv run pre-commit install

# ruff runs without its cache. The cache keeps the import order of a test that was linted before
# the module it imports existed, so a local check passes while CI, which has no cache, fails.
check:
	uv run ruff check --no-cache .
	uv run ruff format --check .
	uv run mypy src eval
	uv run pytest tests/unit -q --cov=longhaul --cov-fail-under=80

ci-local:
	uv sync --locked
	uv run ruff check --no-cache .
	uv run ruff format --check .
	uv run mypy src eval
	uv run pytest tests/unit -q --cov=longhaul --cov-report=term-missing --cov-fail-under=80

itest:
	uv run pytest -m "integration and $(OPT_IN)"

e2e:
	uv run pytest -m "e2e and $(OPT_IN)"

# An empty FakeGitHub on port 8099, for end-to-end runs. Stop it with ctrl-c.
fake-github:
	uv run python tests/fakes/fake_github.py --port 8099

up:
	docker compose up -d --wait

down:
	docker compose down -v

migrate:
	uv run alembic upgrade head

sandbox-image:
	docker build -t longhaul-sandbox:py312 docker/sandbox

# The OpenAPI schema of the console API, then the TypeScript types the console builds from it
# (console/src/api/schema.ts). Run this after any change to src/longhaul/api and commit both
# files. The second step needs the packages: run `npm ci` in console/ once.
api-types:
	uv run python scripts/dump_openapi.py
	npm --prefix console run api:types

# What the console job of CI runs: lint, typecheck, test and build.
console-check:
	npm --prefix console ci
	npm --prefix console run lint
	npm --prefix console run typecheck
	npm --prefix console test
	npm --prefix console run build

# The browser test of the console (LH-808). Playwright starts scripts/seed_demo.sh itself, which
# needs `make up` and `make sandbox-image` first and takes a couple of minutes to seed. To look at
# the seeded console by hand instead, run ./scripts/seed_demo.sh after the build.
console-e2e:
	npm --prefix console ci
	npm --prefix console run build
	npm --prefix console exec -- playwright install chromium
	npm --prefix console run e2e

# The docs site in site/. It needs Quarto (CI pins 1.10.18) and python3 on the PATH. The pages
# are read from docs/, so there is nothing to copy before a render.
site:
	quarto render site

site-preview:
	quarto preview site

# Capture the expression before running pytest. Written inline as a command substitution
# argument, a failing markers.py is ignored and `pytest -m ""` runs every test.
gate: check
	@expr="$$(uv run python scripts/markers.py "$(PHASE)")" \
	|| { echo "gate: PHASE=$(PHASE) is not a phase from 0 to 9" >&2; exit 1; }; \
	test -n "$$expr" || { echo "gate: markers.py printed no expression" >&2; exit 1; }; \
	echo "gate: pytest -m ($$expr) and $(OPT_IN)"; \
	uv run pytest -m "($$expr) and $(OPT_IN)"
