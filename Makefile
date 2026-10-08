# Enterprise Document Intelligence Platform - developer commands.  Run `make` for the list.
# Only targets that work today are defined; later phases add generate-documents, process, evaluate.

SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE ?= docker compose
# Host-side backend commands read configuration from the root .env file.
BACKEND := cd backend && uv run --env-file ../.env

.PHONY: help env require-env setup db-up migrate seed dev dev-api dev-web up down reset-db logs \
        seed-docker test test-backend test-frontend lint format check-ai smoke clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

env: ## Create .env from .env.example with a generated JWT secret (never overwrites)
	@if [ -f .env ]; then \
		echo ".env already exists - left untouched"; \
	else \
		cp .env.example .env; \
		secret=$$(python3 -c "import secrets; print(secrets.token_urlsafe(48))"); \
		sed -i.bak "s|^JWT_SECRET_KEY=.*|JWT_SECRET_KEY=$$secret|" .env && rm -f .env.bak; \
		echo "Created .env with a random JWT_SECRET_KEY. Now set SEED_USER_PASSWORD and GEMINI_API_KEY."; \
	fi

require-env:
	@test -f .env || { echo "Missing .env - run 'make env' first."; exit 1; }

setup: env ## Install backend (uv) and frontend (npm) dependencies
	cd backend && uv sync --frozen
	cd frontend && npm ci --no-audit --no-fund

db-up: require-env ## Start PostgreSQL + pgvector in Docker and wait until healthy
	$(COMPOSE) up -d --wait db

migrate: db-up ## Apply database migrations
	$(BACKEND) alembic upgrade head

seed: migrate ## Create demo departments and one user per role (local/test only)
	$(BACKEND) docintel seed

dev: migrate ## Run API (auto-reload) and Vite dev server on the host -> http://localhost:5173
	$(MAKE) -j2 dev-api dev-web

dev-api:
	$(BACKEND) uvicorn docintel.api.app:create_app --factory --reload --port 8000

dev-web:
	cd frontend && npm run dev

up: require-env ## Build and start the full stack in Docker -> http://localhost:8080
	$(COMPOSE) up -d --build --wait db api web

seed-docker: ## Seed demo users inside the running Docker stack
	$(COMPOSE) exec api docintel seed

down: ## Stop the Docker stack (database volume is kept)
	$(COMPOSE) down

reset-db: ## DESTROY the local database volume
	$(COMPOSE) down -v

logs: ## Follow Docker stack logs
	$(COMPOSE) logs -f --tail=100

test: test-backend test-frontend ## Run all test suites

test-backend: db-up ## Backend unit, integration and security tests (needs Docker Postgres)
	$(BACKEND) pytest

test-frontend: ## Frontend unit/component tests
	cd frontend && npm test

lint: ## Static checks: ruff, mypy --strict, eslint, tsc
	cd backend && uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src tests
	cd frontend && npm run lint && npm run typecheck

format: ## Auto-format backend code
	cd backend && uv run ruff format src tests && uv run ruff check --fix src tests

check-ai: require-env ## Verify GEMINI_API_KEY and configured models with real API calls
	$(BACKEND) docintel check-ai

smoke: ## Smoke-test the running Docker stack through nginx
	./scripts/smoke_test.sh

clean: ## Remove build and tool caches
	rm -rf frontend/dist frontend/node_modules/.tmp backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache
