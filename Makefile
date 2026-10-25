.PHONY: setup check itest e2e up down migrate gate

PHASE ?= 0

setup:
	uv sync
	uv run pre-commit install

check:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src
	uv run pytest tests/unit -q

itest:
	uv run pytest -m integration

e2e:
	uv run pytest -m e2e

up:
	docker compose up -d --wait

down:
	docker compose down -v

migrate:
	uv run alembic upgrade head

gate: check
	uv run pytest -m "$$(uv run python scripts/markers.py $(PHASE))"
