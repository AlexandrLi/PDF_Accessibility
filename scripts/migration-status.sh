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

exec "$ROOT/scripts/with-a11y-python.sh" \
  "$ROOT/scripts/migration_status.py" \
  --course-id "$COURSE_ID" \
  --env "$ENV" \
  "${@:3}"
