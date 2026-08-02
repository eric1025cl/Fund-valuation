#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv311"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

cd "${PROJECT_ROOT}"

if [[ ! -x "${VENV_DIR}/bin/python" ]]; then
  if ! command -v python3.11 >/dev/null 2>&1; then
    echo "python3.11 is required to create ${VENV_DIR}."
    exit 1
  fi

  python3.11 -m venv "${VENV_DIR}"
fi

PYTHON="${VENV_DIR}/bin/python"

if ! "${PYTHON}" - <<'PY' >/dev/null 2>&1
import akshare
import fastapi
import pandas
import uvicorn
PY
then
  "${PYTHON}" -m pip install -r requirements.txt
fi

exec "${PYTHON}" -m uvicorn app:app --host "${HOST}" --port "${PORT}"
