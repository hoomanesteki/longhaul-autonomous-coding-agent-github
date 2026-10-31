.PHONY: setup check ci-local itest e2e fake-github up down migrate sandbox-image gate

PHASE ?= 0

# Tests that only run on request: paid live calls, slow tests and the full chaos suite.
# Gates, `make itest`, `make e2e` and the CI integration job leave them out. chaos_lite is
# the short chaos run that gates and CI keep.
OPT_IN := not live and not slow and not (chaos and not chaos_lite)

setup:
	uv sync
	uv run pre-commit install

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src
	uv run pytest tests/unit -q --cov=longhaul --cov-fail-under=80

ci-local:
	uv sync --locked
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src
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

# Capture the expression before running pytest. Written inline as a command substitution
# argument, a failing markers.py is ignored and `pytest -m ""` runs every test.
gate: check
	@expr="$$(uv run python scripts/markers.py "$(PHASE)")" \
	|| { echo "gate: PHASE=$(PHASE) is not a phase from 0 to 9" >&2; exit 1; }; \
	test -n "$$expr" || { echo "gate: markers.py printed no expression" >&2; exit 1; }; \
	echo "gate: pytest -m ($$expr) and $(OPT_IN)"; \
	uv run pytest -m "($$expr) and $(OPT_IN)"
