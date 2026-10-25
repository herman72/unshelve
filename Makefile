.DEFAULT_GOAL := help
SHELL := /bin/bash
API := apps/api
COMPOSE := docker compose

.PHONY: help setup hooks db up down logs fmt lint typecheck test eval eval-live eval-baseline site-search openapi openapi-check check

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Install Python and Node dependencies
	cd $(API) && uv sync
	pnpm install

hooks: ## Install the git pre-commit hooks
	uvx pre-commit install

db: ## Start Postgres only (the tests need it)
	$(COMPOSE) up -d --wait db

up: ## Build and start the whole stack
	$(COMPOSE) up -d --build --wait

down: ## Stop the stack (data is kept)
	$(COMPOSE) down

logs: ## Follow the logs
	$(COMPOSE) logs -f --tail=100

fmt: ## Format and apply safe fixes
	cd $(API) && uv run ruff format . && uv run ruff check --fix .

lint: ## Lint Python and TypeScript
	cd $(API) && uv run ruff check . && uv run ruff format --check .
	pnpm -r lint

typecheck: ## Type-check Python and TypeScript
	cd $(API) && uv run pyright
	pnpm -r typecheck

test: db ## Run the Python tests
	cd $(API) && uv run pytest

eval: db ## Measure search on the golden set with the recorded vectors (the gate of CI)
	cd $(API) && uv run pytest -q tests/evals/test_search_gate.py

eval-live: db ## Record the vectors of the golden set again, with the embedder of .env
	cd $(API) && uv run --env-file ../../.env pytest -m live -o addopts="" -q tests/evals/test_search_gate.py

eval-baseline: db ## Write the scores of the recorded vectors as the new baseline
	cd $(API) && UNSHELVE_EVAL_BASELINE=write uv run pytest -q tests/evals/test_search_gate.py

site-search: db ## Write again the files of packages/site-search that come from the server
	cd $(API) && UNSHELVE_SITE_SEARCH=write uv run pytest -q tests/publish/test_site_search.py

openapi: ## Regenerate openapi.json and the TypeScript types
	cd $(API) && uv run unshelve openapi
	pnpm --filter @unshelve/api-client generate

openapi-check: openapi ## Fail when the committed API contract is stale
	git diff --exit-code -- $(API)/openapi.json packages/api-client/src/schema.d.ts

check: lint typecheck test openapi-check ## Everything CI runs
