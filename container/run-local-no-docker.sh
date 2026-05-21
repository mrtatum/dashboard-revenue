#!/usr/bin/env bash
# Run the dashboard on this laptop without Docker and without OneDrive.
#
# Bakes the dashboard from the existing local Sources/ folder (one directory
# above this one), then serves it on http://localhost:8080 via Flask.
# Use this to verify the bake + serve code path before setting up Docker or
# OneDrive auth.
#
# Requirements: Python 3.10+ already installed on the Mac.

set -euo pipefail
cd "$(dirname "$0")"

PROJECT_ROOT="$(cd .. && pwd)"
SOURCES="$PROJECT_ROOT/Sources"
TEMPLATE="$(pwd)/template/Pipeline_Dashboard.template.html"
RUNTIME_DIR="$(pwd)/.local-runtime"
OUTPUT_HTML="$RUNTIME_DIR/Pipeline_Dashboard.html"
VENV="$RUNTIME_DIR/.venv"
PORT=8080

if [[ ! -d "$SOURCES" ]]; then
    echo "❌ Expected local sources at: $SOURCES" >&2
    echo "   This script reads xlsx from your existing Dashboard_Revenue/Sources/ folder." >&2
    exit 1
fi

mkdir -p "$RUNTIME_DIR"

if [[ ! -d "$VENV" ]]; then
    echo "→ Creating virtualenv at $VENV …"
    python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

cd "$PROJECT_ROOT"

echo "→ Baking dashboard from $SOURCES …"
SOURCES_DIR="$SOURCES" \
TEMPLATE_PATH="$TEMPLATE" \
OUTPUT_PATH="$OUTPUT_HTML" \
python3 -m container.app.bake

echo
echo "→ Starting Flask on http://localhost:$PORT …  (Ctrl+C to stop)"
SOURCES_DIR="$SOURCES" \
TEMPLATE_PATH="$TEMPLATE" \
OUTPUT_PATH="$OUTPUT_HTML" \
BAKE_ON_START=0 \
PORT="$PORT" \
MS_CLIENT_ID="local-test" \
MS_REFRESH_TOKEN="local-test" \
MS_TENANT="common" \
ONEDRIVE_DRIVE_ID="local-test" \
ONEDRIVE_ITEM_ID="local-test" \
MS_SECRETS_PATH="$RUNTIME_DIR/refresh_token" \
python3 -m container.app.server
