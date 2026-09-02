.PHONY: install fmt lint typecheck test check
install:
	pip install -e ".[dev]"
fmt:
	ruff format src tests && ruff check --fix src tests
lint:
	ruff check src tests
typecheck:
	mypy
test:
	pytest -q
check: lint typecheck test