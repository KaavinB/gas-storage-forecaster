-include .env
export

PY := uv run python

.DEFAULT_GOAL := help
.PHONY: help setup up down migrate psql test lint ingest ingest-full dq backtest forecast alerts daily site site-data

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

forecast: ## Retrain on all published data and store today's forecasts
	$(PY) -m models.forecast

alerts: ## Evaluate alert rules, log them and email new ones
	$(PY) -m alerts.run

daily: ingest dq forecast alerts site-data ## The full daily pipeline, as run by GitHub Actions

site-data:
	$(PY) -m dashboards.export

site: site-data ## Export dashboard data and serve it at http://localhost:8000
	cd dashboards/site && python3 -m http.server 8000

psql: ## Open a psql shell inside the container
	docker compose exec db psql -U $(POSTGRES_USER) -d $(POSTGRES_DB)

test: ## Run the test suite
	uv run pytest

lint: ## Lint with ruff
	uv run ruff check .
