#!/usr/bin/env bash
# Start a channels preview migration CodeBuild run.
set -euo pipefail

PROJECT_NAME="${MIGRATE_CODEBUILD_PROJECT:-channels-worksheet-a11y-migrate}"
COURSE_ID=""
ENV="dev"
DRY_RUN="false"
SKIP_CDN="false"
SOURCE_VERSION="${SOURCE_VERSION:-}"

usage() {
  cat <<EOF
Usage: $0 --course-id <id> [--env dev|prod] [--dry-run] [--skip-cdn-invalidation] [--source-version branch]

Starts CodeBuild project $PROJECT_NAME with AUTO_CHAPTERS enabled.
Re-run the same command to resume from S3 progress (courses/<id>/.a11y-migration-progress.json).
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --course-id) COURSE_ID="$2"; shift 2 ;;
    --env) ENV="$2"; shift 2 ;;
    --dry-run) DRY_RUN="true"; shift ;;
    --skip-cdn-invalidation) SKIP_CDN="true"; shift ;;
    --source-version) SOURCE_VERSION="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1"; usage; exit 1 ;;
  esac
done

if [ -z "$COURSE_ID" ]; then
  usage
  exit 1
fi

echo "Note: run only one migration build per course at a time (S3 progress file)."

OVERRIDES=(
  "name=COURSE_ID,value=${COURSE_ID},type=PLAINTEXT"
  "name=ENV,value=${ENV},type=PLAINTEXT"
  "name=AUTO_CHAPTERS,value=true,type=PLAINTEXT"
  "name=SKIP_IF_AUDITED,value=true,type=PLAINTEXT"
  "name=DRY_RUN,value=${DRY_RUN},type=PLAINTEXT"
  "name=SKIP_CDN_INVALIDATION,value=${SKIP_CDN},type=PLAINTEXT"
)

ARGS=(aws codebuild start-build --project-name "$PROJECT_NAME")
for override in "${OVERRIDES[@]}"; do
  ARGS+=(--environment-variables-override "$override")
done
if [ -n "$SOURCE_VERSION" ]; then
  ARGS+=(--source-version "$SOURCE_VERSION")
fi

echo "Starting CodeBuild: ${ARGS[*]}"
BUILD_JSON="$("${ARGS[@]}")"
BUILD_ID="$(echo "$BUILD_JSON" | jq -r '.build.id')"
echo "Build ID: $BUILD_ID"
echo "Monitor: aws codebuild batch-get-builds --ids $BUILD_ID"
echo "Logs:    CloudWatch log group /aws/codebuild/$PROJECT_NAME"
