#!/usr/bin/env bash
set -euo pipefail

cd /app

bash scripts/compose-install.sh

echo "[compose-beat] starting celery beat..."
exec celery -A review_agent.worker.celery_app beat --loglevel=info
