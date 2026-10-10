.DEFAULT_GOAL := help
SHELL := /bin/bash
API := apps/api
COMPOSE := docker compose

.PHONY: help setup hooks db up down logs fmt lint typecheck test openapi openapi-check check

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

openapi: ## Regenerate openapi.json and the TypeScript types
	cd $(API) && uv run unshelve openapi
	pnpm --filter @unshelve/api-client generate

openapi-check: openapi ## Fail when the committed API contract is stale
	git diff --exit-code -- $(API)/openapi.json packages/api-client/src/schema.d.ts

check: lint typecheck test openapi-check ## Everything CI runs
