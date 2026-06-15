.PHONY: install dev test lint fmt typecheck check migrate revision up down

install:
	uv sync

dev:
	uv run uvicorn app.main:app --reload

test:
	uv run pytest

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

typecheck:
	uv run mypy app

# Run this before considering any change done (CLAUDE.md §11).
check: lint typecheck test

migrate:
	uv run alembic upgrade head

revision:
	uv run alembic revision --autogenerate -m "$(m)"

up:
	docker compose up --build

down:
	docker compose down
