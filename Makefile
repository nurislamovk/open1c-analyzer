.PHONY: install lint format typecheck test check

install:
	uv sync --extra dev

lint:
	uv run ruff check .

format:
	uv run ruff format .

typecheck:
	uv run mypy src

test:
	uv run pytest

check: lint typecheck test
