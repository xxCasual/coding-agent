#!/usr/bin/env bash
# Capture a pytest run for before/after evidence. Executed only via Executor.
set -euo pipefail
cd "${1:-.}"
python -m pytest -q "${@:2}"
