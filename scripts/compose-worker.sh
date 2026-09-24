#!/usr/bin/env bash
set -euo pipefail

cd /app

bash scripts/compose-install.sh

echo "[compose-worker] starting celery worker..."
exec celery -A review_agent.worker.celery_app worker --loglevel=info --pool=prefork
