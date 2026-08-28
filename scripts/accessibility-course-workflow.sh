#!/usr/bin/env bash
# Prepare, publish, or roll back an accessibility issue-map course run.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMMAND="${1:-}"
if [[ -z "$COMMAND" ]]; then
  echo "Usage: $0 prepare <course-id> | plan <manifest> | publish <manifest> --approved-sha <sha> | rollback <report>" >&2
  exit 2
fi
shift

case "$COMMAND" in
  prepare)
    COURSE_ID="${1:-}"
    if [[ -z "$COURSE_ID" ]]; then
      echo "prepare requires a course ID" >&2
      exit 2
    fi
    shift
    exec "$ROOT/scripts/sweep-accessibility-issue-map.sh" \
      --course-id "$COURSE_ID" \
      --resume \
      "$@"
    ;;
  plan)
    MANIFEST="${1:-}"
    if [[ -z "$MANIFEST" ]]; then
      echo "plan requires a compact manifest" >&2
      exit 2
    fi
    shift
    exec "$ROOT/scripts/with-a11y-python.sh" \
      "$ROOT/scripts/replace_accessibility_issue_map_pdfs.py" \
      plan \
      --manifest "$MANIFEST" \
      "$@"
    ;;
  publish)
    MANIFEST="${1:-}"
    if [[ -z "$MANIFEST" ]]; then
      echo "publish requires a compact manifest" >&2
      exit 2
    fi
    shift
    exec "$ROOT/scripts/with-a11y-python.sh" \
      "$ROOT/scripts/replace_accessibility_issue_map_pdfs.py" \
      apply \
      --manifest "$MANIFEST" \
      "$@"
    ;;
  rollback)
    REPORT="${1:-}"
    if [[ -z "$REPORT" ]]; then
      echo "rollback requires a replacement report" >&2
      exit 2
    fi
    shift
    exec "$ROOT/scripts/with-a11y-python.sh" \
      "$ROOT/scripts/replace_accessibility_issue_map_pdfs.py" \
      rollback \
      --report "$REPORT" \
      "$@"
    ;;
  *)
    echo "Unknown command: $COMMAND" >&2
    exit 2
    ;;
esac
