#!/usr/bin/env bash
# Shared dependency install for compose api / worker / beat containers.
set -euo pipefail

cd /app

PIP_INDEX_URL="${PIP_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}"
PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-pypi.tuna.tsinghua.edu.cn}"
export PIP_DEFAULT_TIMEOUT="${PIP_DEFAULT_TIMEOUT:-300}"
PIP_ARGS=(-i "$PIP_INDEX_URL" --trusted-host "$PIP_TRUSTED_HOST")

if python -c "import review_agent, celery, pytest, fastapi" >/dev/null 2>&1; then
  echo "[compose-install] deps already present; refreshing editable package only..."
  pip install "${PIP_ARGS[@]}" -e . --no-deps -q
  echo "[compose-install] done (skipped full install)."
  exit 0
fi

echo "[compose-install] using index: $PIP_INDEX_URL"
echo "[compose-install] installing review-agent with dev extras..."
pip install "${PIP_ARGS[@]}" -e '.[dev]'

echo "[compose-install] installing eval sample requirements..."
for req in /app/eval/samples/*/requirements.txt; do
  if [[ -f "$req" ]]; then
    echo "[compose-install]   -> $req"
    pip install "${PIP_ARGS[@]}" -r "$req"
  fi
done

echo "[compose-install] done."
