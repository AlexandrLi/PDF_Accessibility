#!/usr/bin/env bash
# Create or update the CodeBuild project for course-wide preview migration.
# See docs/CHANNELS_WORKSHEET_A11Y_PLAN.md §2.9
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PROJECT_NAME="${MIGRATE_CODEBUILD_PROJECT:-channels-worksheet-a11y-migrate}"
ROLE_NAME="${PROJECT_NAME}-role"
POLICY_NAME="${PROJECT_NAME}-policy"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
SOURCE_VERSION="${SOURCE_VERSION:-main}"
GITHUB_URL="${GITHUB_URL:-}"
COMPUTE_TYPE="${COMPUTE_TYPE:-BUILD_GENERAL1_MEDIUM}"
TIMEOUT_MINUTES="${TIMEOUT_MINUTES:-480}"
POLICY_DIR="$ROOT/policies"

red() { printf '\033[0;31m%s\033[0m\n' "$*"; }
green() { printf '\033[0;32m%s\033[0m\n' "$*"; }
info() { printf '→ %s\n' "$*"; }

if [ -z "$GITHUB_URL" ]; then
  red "GITHUB_URL is required (HTTPS clone URL for PDF_Accessibility_fork)."
  echo "Example: export GITHUB_URL=https://github.com/your-org/PDF_Accessibility_fork.git"
  exit 1
fi

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

info "Discovering PDF accessibility stack outputs..."
# shellcheck disable=SC1091
eval "$(./scripts/discover-migrate-env.sh)"

if [ -z "${A11Y_BUCKET:-}" ] || [ "$A11Y_BUCKET" = "None" ]; then
  red "Could not resolve A11Y_BUCKET from CloudFormation stack PDFAccessibility"
  exit 1
fi
if [ -z "${STATE_MACHINE_ARN:-}" ] || [ "$STATE_MACHINE_ARN" = "None" ]; then
  red "Could not resolve STATE_MACHINE_ARN from CloudFormation stack PDFAccessibility"
  exit 1
fi

info "Setting up IAM role: $ROLE_NAME"
if aws iam get-role --role-name "$ROLE_NAME" >/dev/null 2>&1; then
  ROLE_ARN="$(aws iam get-role --role-name "$ROLE_NAME" --query Role.Arn --output text)"
  green "Role already exists: $ROLE_ARN"
else
  ROLE_ARN="$(aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document "file://${POLICY_DIR}/codebuild-trust-policy.json" \
    --query Role.Arn --output text)"
  green "Created role: $ROLE_ARN"
  sleep 10
fi

POLICY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/${POLICY_NAME}"
if aws iam get-policy --policy-arn "$POLICY_ARN" >/dev/null 2>&1; then
  info "Updating policy $POLICY_NAME"
  aws iam create-policy-version \
    --policy-arn "$POLICY_ARN" \
    --policy-document "file://${POLICY_DIR}/channels-migrate-codebuild-policy.json" \
    --set-as-default >/dev/null
  green "Policy version updated: $POLICY_ARN"
else
  POLICY_ARN="$(aws iam create-policy \
    --policy-name "$POLICY_NAME" \
    --policy-document "file://${POLICY_DIR}/channels-migrate-codebuild-policy.json" \
    --description "Channels worksheet preview migration CodeBuild policy" \
    --query Policy.Arn --output text)"
  green "Created policy: $POLICY_ARN"
fi

if ! aws iam list-attached-role-policies --role-name "$ROLE_NAME" \
  --query "AttachedPolicies[?PolicyArn=='${POLICY_ARN}'].PolicyArn | [0]" \
  --output text | grep -q "$POLICY_ARN"; then
  aws iam attach-role-policy --role-name "$ROLE_NAME" --policy-arn "$POLICY_ARN"
  green "Attached policy to role"
fi

ENV_VARS="$(jq -n \
  --arg a11y "$A11Y_BUCKET" \
  --arg sm "$STATE_MACHINE_ARN" \
  --arg channels "${CHANNELS_DATA_BUCKET:-channels-data-dev}" \
  --arg cdn "${CHANNELS_CLOUDFRONT_DISTRIBUTION_ID:-E27O7BO97BHXFO}" \
  '[
    {"name":"A11Y_BUCKET","value":$a11y,"type":"PLAINTEXT"},
    {"name":"STATE_MACHINE_ARN","value":$sm,"type":"PLAINTEXT"},
    {"name":"CHANNELS_DATA_BUCKET","value":$channels,"type":"PLAINTEXT"},
    {"name":"CHANNELS_CLOUDFRONT_DISTRIBUTION_ID","value":$cdn,"type":"PLAINTEXT"},
    {"name":"AUTO_CHAPTERS","value":"true","type":"PLAINTEXT"},
    {"name":"SKIP_IF_AUDITED","value":"true","type":"PLAINTEXT"}
  ]')"

ENVIRONMENT="$(jq -n \
  --arg image "aws/codebuild/amazonlinux-x86_64-standard:5.0" \
  --arg compute "$COMPUTE_TYPE" \
  --argjson envvars "$ENV_VARS" \
  '{
    type: "LINUX_CONTAINER",
    image: $image,
    computeType: $compute,
    privilegedMode: false,
    environmentVariables: $envvars
  }')"

SOURCE="$(jq -n \
  --arg url "$GITHUB_URL" \
  '{
    type: "GITHUB",
    location: $url,
    buildspec: "buildspec-migrate.yml",
    gitCloneDepth: 1
  }')"

ARTIFACTS='{"type":"CODEBUILD","packaging":"NONE","name":"channels-worksheet-a11y-migrate-artifacts"}'

info "Creating/updating CodeBuild project: $PROJECT_NAME"
if aws codebuild batch-get-projects --names "$PROJECT_NAME" --query 'projects[0].name' --output text 2>/dev/null | grep -q "$PROJECT_NAME"; then
  aws codebuild update-project \
    --name "$PROJECT_NAME" \
    --source "$SOURCE" \
    --source-version "$SOURCE_VERSION" \
    --artifacts "$ARTIFACTS" \
    --environment "$ENVIRONMENT" \
    --service-role "$ROLE_ARN" \
    --timeout-in-minutes "$TIMEOUT_MINUTES" \
    --queued-timeout-in-minutes 60 \
    --output json >/dev/null
  green "Updated CodeBuild project $PROJECT_NAME"
else
  aws codebuild create-project \
    --name "$PROJECT_NAME" \
    --source "$SOURCE" \
    --source-version "$SOURCE_VERSION" \
    --artifacts "$ARTIFACTS" \
    --environment "$ENVIRONMENT" \
    --service-role "$ROLE_ARN" \
    --timeout-in-minutes "$TIMEOUT_MINUTES" \
    --queued-timeout-in-minutes 60 \
    --output json >/dev/null
  green "Created CodeBuild project $PROJECT_NAME"
fi

cat <<EOF

Done.

Prerequisites:
  - CodeBuild must be connected to GitHub (OAuth) for GITHUB_URL, or use a source
    provider your account already supports.
  - Run only ONE migration build per course at a time. Overlapping builds race on
    courses/<courseId>/.a11y-migration-progress.json (last write wins).
  - For prod: re-run this script with CHANNELS_DATA_BUCKET=channels-data-prod and
    the prod CloudFront distribution ID before starting prod builds.

Start a dry run:
  ./scripts/start-migration-build.sh --course-id biochemistry --env dev --dry-run

Start live migration (auto-chapters, resumes from S3 progress):
  ./scripts/start-migration-build.sh --course-id biochemistry --env dev

Check progress:
  ./scripts/migration-status.sh biochemistry dev

Logs: CloudWatch → /aws/codebuild/$PROJECT_NAME
EOF
