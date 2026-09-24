PY_ENV ?= review-agent
UV := UV_PROJECT_ENVIRONMENT=$(PY_ENV) uv

.PHONY: setup test lint check dev-api dev-frontend agent

setup:
	$(UV) sync --extra dev
	npm --prefix frontend install

test:
	$(UV) run --extra dev pytest

lint:
	$(UV) run --extra dev ruff check

check: test lint

dev-api:
	$(UV) run uvicorn review_agent.api.app:app --reload

dev-frontend:
	npm --prefix frontend run dev

agent:
	$(UV) run review-agent agent .
