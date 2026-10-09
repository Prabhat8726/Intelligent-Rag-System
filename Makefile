# Enterprise Document Intelligence Platform - developer commands.  Run `make` for the list.
# Only targets that work today are defined.

SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE ?= docker compose
# Host-side backend commands read configuration from the root .env file.
BACKEND := cd backend && uv run --env-file ../.env
# API used by `make process`: the host API (make dev) by default; use http://localhost:8080 for make up.
API_URL ?= http://localhost:8000
DATASET ?= ../synthetic_data/generated
# Extra flags for `make process`, e.g. INGEST_FLAGS=--require-completed (CI).
INGEST_FLAGS ?=

.PHONY: help env require-env setup db-up migrate seed dev dev-api dev-worker dev-web up down \
        reset-db logs seed-docker generate-documents process seed-knowledge reembed worker test \
        test-backend test-frontend lint format check-ai check-ocr llm-usage evaluate match smoke \
        clean

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

dev: migrate ## Run API (auto-reload), worker and Vite on the host -> http://localhost:5173
	$(MAKE) -j3 dev-api dev-worker dev-web

dev-api:
	$(BACKEND) uvicorn docintel.api.app:create_app --factory --reload --port 8000

dev-worker:
	$(BACKEND) docintel worker

worker: require-env ## Process queued jobs on the host until the queue is empty, then exit
	$(BACKEND) docintel worker --until-idle

dev-web:
	cd frontend && npm run dev

up: require-env ## Build and start the full stack in Docker -> http://localhost:8080
	$(COMPOSE) up -d --build --wait db api worker web

seed-docker: ## Seed demo users inside the running Docker stack
	$(COMPOSE) exec api docintel seed

generate-documents: ## Generate synthetic POs, invoices and delivery notes with ground truth
	cd backend && uv run docintel generate-documents --output $(DATASET)

process: require-env ## Upload the synthetic dataset through the API and wait for processing
	$(BACKEND) docintel ingest $(DATASET) --api-url $(API_URL) $(INGEST_FLAGS)

seed-knowledge: require-env ## Load knowledge_base/ (policies, procedures, FAQs) through the API
	$(BACKEND) docintel knowledge-ingest ../knowledge_base --api-url $(API_URL) --email admin@docintel.local

reembed: migrate ## Add vectors of the configured embedding model to indexed chunks
	$(BACKEND) docintel reembed

down: ## Stop the Docker stack (volumes are kept)
	$(COMPOSE) down

reset-db: ## DESTROY the local database and document storage volumes
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

check-ocr: require-env ## Verify the Tesseract OCR engine and configured languages
	$(BACKEND) docintel check-ocr

llm-usage: require-env ## LLM requests, tokens and estimated cost per day (last 7 days)
	$(BACKEND) docintel llm-usage --days 7

match: migrate ## Re-run comparisons, rules and review tasks for every processed document
	$(BACKEND) docintel match

evaluate: db-up ## Run all evaluation suites (OCR ... retrieval) -> evaluation/reports (several minutes)
	$(BACKEND) docintel evaluate --output ../evaluation/reports

smoke: ## Smoke-test the running Docker stack through nginx
	./scripts/smoke_test.sh

clean: ## Remove build and tool caches
	rm -rf frontend/dist frontend/node_modules/.tmp backend/.pytest_cache backend/.mypy_cache backend/.ruff_cache
