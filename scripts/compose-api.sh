#!/usr/bin/env bash
set -euo pipefail

cd /app

bash scripts/compose-install.sh

echo "[compose-api] running alembic..."
alembic upgrade head

echo "[compose-api] ensuring LangGraph checkpoint tables..."
python -c "import asyncio, os; from review_agent.services.graph_checkpointer import setup_postgres_checkpointer; asyncio.run(setup_postgres_checkpointer(os.environ['REVIEW_AGENT_DATABASE_URL']))"

echo "[compose-api] seeding workspace registry..."
python scripts/compose-seed-registry.py

echo "[compose-api] starting uvicorn..."
exec uvicorn review_agent.api.app:app --host 0.0.0.0 --port 8000
