#!/usr/bin/env bash
# Print migration env vars from the deployed PDFAccessibility stack.
set -euo pipefail

REGION="${AWS_DEFAULT_REGION:-us-east-1}"
STACK="${CDK_STACK_NAME:-PDFAccessibility}"

A11Y_BUCKET="$(aws cloudformation describe-stack-resources \
  --stack-name "$STACK" \
  --region "$REGION" \
  --query "StackResources[?ResourceType=='AWS::S3::Bucket'].PhysicalResourceId | [0]" \
  --output text 2>/dev/null || true)"

STATE_MACHINE_ARN="$(aws cloudformation describe-stack-resources \
  --stack-name "$STACK" \
  --region "$REGION" \
  --query "StackResources[?ResourceType=='AWS::StepFunctions::StateMachine'].PhysicalResourceId | [0]" \
  --output text 2>/dev/null || true)"

if [ -z "$A11Y_BUCKET" ] || [ "$A11Y_BUCKET" = "None" ]; then
  A11Y_BUCKET="$(aws s3 ls | awk '/pdfaccessibilitybucket1/ {print $3; exit}')"
fi

cat <<EOF
export AWS_DEFAULT_REGION=$REGION
export A11Y_BUCKET=${A11Y_BUCKET}
export STATE_MACHINE_ARN=${STATE_MACHINE_ARN}
export CHANNELS_DATA_BUCKET=channels-data-dev
export CHANNELS_CLOUDFRONT_DISTRIBUTION_ID=E27O7BO97BHXFO
EOF
