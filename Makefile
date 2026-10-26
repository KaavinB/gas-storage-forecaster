-include .env
export

PY := uv run python

.DEFAULT_GOAL := help
.PHONY: help setup up down migrate psql test lint ingest ingest-full dq backtest

help: ## List available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' Makefile | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install Python 3.12 deps with uv and create .env if missing
	uv sync
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example - fill in your keys")

up: ## Start Postgres and wait until it is healthy
	docker compose up -d --wait

down: ## Stop Postgres (data volume is kept)
	docker compose down

migrate: up ## Create/refresh tables and views
	$(PY) -m core.migrate

ingest: migrate ## Incremental load of storage, Henry Hub and degree days
	$(PY) -m ingestion.run

ingest-full: migrate ## Reload full history (2010 onward)
	$(PY) -m ingestion.run --full

dq: ## Run data-quality checks (exits non-zero on failure)
	$(PY) -m ingestion.dq

backtest: ## Walk-forward backtest (~3 min); writes reports/backtest_results.md
	$(PY) -m models.backtest

psql: ## Open a psql shell inside the container
	docker compose exec db psql -U $(POSTGRES_USER) -d $(POSTGRES_DB)

test: ## Run the test suite
	uv run pytest

lint: ## Lint with ruff
	uv run ruff check .
