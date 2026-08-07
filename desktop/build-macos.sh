#!/usr/bin/env sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
cd "$REPO_ROOT"

python3 -m pip install -r requirements.txt

python3 -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --name FundValuation \
  --add-data "web:web" \
  --hidden-import app \
  --hidden-import fundval.providers \
  --hidden-import fundval.service \
  --hidden-import fundval.store \
  --hidden-import fundval.valuation \
  --collect-all akshare \
  --collect-all pandas \
  desktop/main.py

echo "macOS desktop build created under dist/FundValuation.app"
