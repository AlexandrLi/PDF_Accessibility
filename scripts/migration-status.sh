#!/usr/bin/env bash
# Print preview migration progress from S3 for a course.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COURSE_ID="${1:-}"
ENV="${ENV:-dev}"

if [ -z "$COURSE_ID" ]; then
  echo "Usage: $0 <course-id> [dev|prod]"
  exit 1
fi

if [ "${2:-}" != "" ]; then
  ENV="$2"
fi

if [ -f ".env.migrate" ]; then
  # shellcheck disable=SC1091
  source .env.migrate
fi

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r scripts/requirements-migrate.txt

exec python3 scripts/migration_status.py --course-id "$COURSE_ID" --env "$ENV" "${@:3}"
