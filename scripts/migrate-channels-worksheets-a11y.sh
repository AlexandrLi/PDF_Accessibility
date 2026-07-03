#!/usr/bin/env bash
# Channels worksheet a11y migration — see docs/CHANNELS_WORKSHEET_A11Y_PLAN.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

pip install -q -r scripts/requirements-migrate.txt

export PYTHONUNBUFFERED=1
exec python3 scripts/migrate_channels_worksheets.py "$@"
