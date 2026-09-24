#!/usr/bin/env bash
# Smoke-check a running docker compose stack (no model API calls).
set -euo pipefail

API_URL="${REVIEW_AGENT_API_URL:-http://127.0.0.1:8000}"
MAX_WAIT="${COMPOSE_SMOKE_MAX_WAIT:-600}"

echo "[compose-smoke] waiting for ${API_URL}/api/health (max ${MAX_WAIT}s)..."
deadline=$(( $(date +%s) + MAX_WAIT ))
until curl -sf "${API_URL}/api/health" >/dev/null; do
  if (( $(date +%s) >= deadline )); then
    echo "[compose-smoke] ERROR: health check timed out" >&2
    docker compose ps 2>/dev/null || true
    exit 1
  fi
  sleep 3
done

health=$(curl -sf "${API_URL}/api/health")
echo "[compose-smoke] health: ${health}"

workspaces_json=$(curl -sf "${API_URL}/api/workspaces")
echo "[compose-smoke] workspaces: ${workspaces_json}"

demo_count=$(python3 -c "
import json, sys
data = json.loads(sys.argv[1])
items = data if isinstance(data, list) else data.get('workspaces', [])
ids = [w.get('workspace_id', w.get('id', '')) for w in items]
print(sum(1 for i in ids if str(i).startswith('demo-')))
" "${workspaces_json}")

if [[ "${demo_count}" -lt 1 ]]; then
  echo "[compose-smoke] ERROR: expected at least one demo-* workspace, got ${demo_count}" >&2
  exit 1
fi

echo "[compose-smoke] found ${demo_count} demo workspace(s)"

if command -v docker >/dev/null 2>&1; then
  echo "[compose-smoke] compose service status:"
  docker compose ps api worker beat 2>/dev/null || docker compose ps
fi

echo "[compose-smoke] OK"
