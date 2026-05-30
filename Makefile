.PHONY: install dev test lint smoke

install:
	python -m pip install -e '.[dev]'

dev:
	uvicorn auto_assign.main:app --host $${AUTO_ASSIGN_HOST:-0.0.0.0} --port $${AUTO_ASSIGN_PORT:-8090} --reload

test:
	pytest -q

lint:
	ruff check src tests

smoke:
	python -m compileall src
	pytest -q
