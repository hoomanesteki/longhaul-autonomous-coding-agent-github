.PHONY: setup check ci-local itest e2e fake-github up down migrate sandbox-image api-types console-check console-e2e site site-preview gate demo

PHASE ?= 0

# Tests that only run on request: paid live calls, slow tests and the full chaos suite.
# Gates, `make itest`, `make e2e` and the CI integration job leave them out. chaos_lite is
# the short chaos run that gates and CI keep.
OPT_IN := not live and not slow and not (chaos and not chaos_lite)

# A fresh clone has no .env, and the database URL has no built-in default, so setup copies the
# local defaults once. An existing .env is never touched.
setup:
	uv sync
	uv run pre-commit install
	test -f .env || cp .env.example .env

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
# needs `make up` first and takes a couple of minutes to seed. The seeded tasks run in real
# sandboxes, so the image is a prerequisite; docker reuses it when it is built (LH-891). To look at
# the seeded console by hand instead, run ./scripts/seed_demo.sh after the build.
console-e2e: sandbox-image
	npm --prefix console ci
	npm --prefix console run build
	npm --prefix console exec -- playwright install chromium
	npm --prefix console run e2e

# One finished task on the calc fixture with the scripted model, so no GitHub account or key is
# needed. `make up` starts the services. The sandbox image and the tables are built here because
# `make up` does not cover them. scripts/demo.sh starts the worker, runs the task, prints the diff
# and stops the worker.
demo: sandbox-image migrate
	./scripts/demo.sh

# The docs site in site/. It needs Quarto (CI pins 1.10.18) and python3 on the PATH. The pages
# are read from docs/, so there is nothing to copy before a render.
site:
	quarto render site

site-preview:
	quarto preview site

# The phases whose gate also runs the console: phase 8 built it, and phase 9 includes it.
# Integration checkpoint 8 names its unit tests and the Playwright main path (LH-891).
CONSOLE_PHASES := 8 9

# Capture the expression before running pytest. Written inline as a command substitution
# argument, a failing markers.py is ignored and `pytest -m ""` runs every test.
gate: check
	@expr="$$(uv run python scripts/markers.py "$(PHASE)")" \
	|| { echo "gate: PHASE=$(PHASE) is not a phase from 0 to 9" >&2; exit 1; }; \
	test -n "$$expr" || { echo "gate: markers.py printed no expression" >&2; exit 1; }; \
	echo "gate: pytest -m ($$expr) and $(OPT_IN)"; \
	uv run pytest -m "($$expr) and $(OPT_IN)"
	@if [ -n "$(filter $(PHASE),$(CONSOLE_PHASES))" ]; then \
	  echo "gate: console checks for PHASE=$(PHASE)"; \
	  $(MAKE) console-check console-e2e; \
	fi
