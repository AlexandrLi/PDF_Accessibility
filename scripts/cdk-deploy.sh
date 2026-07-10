#!/usr/bin/env bash
# CDK deploy helper — Colima + venv. See docs/REDEPLOY.md
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COLIMA_SOCKET="${HOME}/.colima/default/docker.sock"
DOCKER_CONTEXT="${DOCKER_CONTEXT:-colima}"

ensure_colima() {
  if ! command -v colima >/dev/null 2>&1; then
    echo "colima not found. Install: brew install colima"
    exit 1
  fi
  if ! colima status >/dev/null 2>&1; then
    echo "Starting Colima..."
    colima start
  fi
}

ensure_docker_cli() {
  if command -v docker >/dev/null 2>&1; then
    return
  fi
  if brew list docker >/dev/null 2>&1; then
    echo "Linking Homebrew docker CLI..."
    brew link --overwrite docker >/dev/null 2>&1 || brew link docker
    return
  fi
  echo "docker CLI not found. Install: brew install docker"
  exit 1
}

configure_docker_for_colima() {
  ensure_colima
  ensure_docker_cli

  if [ ! -S "$COLIMA_SOCKET" ]; then
    echo "Colima docker socket missing: $COLIMA_SOCKET"
    echo "Try: colima stop && colima start"
    exit 1
  fi

  # Prefer docker context over DOCKER_HOST (CDK/buildx use the docker CLI).
  unset DOCKER_HOST
  if docker context inspect "$DOCKER_CONTEXT" >/dev/null 2>&1; then
    docker context use "$DOCKER_CONTEXT" >/dev/null
  else
    docker context create "$DOCKER_CONTEXT" --docker "host=unix://${COLIMA_SOCKET}"
    docker context use "$DOCKER_CONTEXT" >/dev/null
  fi

  export BUILDX_NO_DEFAULT_ATTESTATIONS=1
  docker info >/dev/null
  echo "Docker OK (context: $(docker context show), server: $(docker info -f '{{.ServerVersion}}'))"
}

ensure_cdk_venv() {
  if [ ! -d ".venv-cdk" ]; then
    python3 -m venv .venv-cdk
    # shellcheck disable=SC1091
    source .venv-cdk/bin/activate
    pip install -q -r requirements.txt
  else
    # shellcheck disable=SC1091
    source .venv-cdk/bin/activate
  fi

  if ! command -v cdk >/dev/null 2>&1; then
    echo "CDK CLI not found. Install: npm install -g aws-cdk"
    exit 1
  fi
}

usage() {
  cat <<EOF
Usage: $0 [cdk args...]

Examples:
  $0                          # cdk deploy PDFAccessibility
  $0 diff
  $0 deploy PDFAccessibility --require-approval never

Requires: colima (running), docker CLI, aws-cdk, AWS credentials.
EOF
}

if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  usage
  exit 0
fi

configure_docker_for_colima
ensure_cdk_venv

export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-us-east-1}"
export CDK_DEFAULT_ACCOUNT="${CDK_DEFAULT_ACCOUNT:-$(aws sts get-caller-identity --query Account --output text)}"
export CDK_DEFAULT_REGION="${CDK_DEFAULT_REGION:-$AWS_DEFAULT_REGION}"

if [ $# -eq 0 ]; then
  set -- deploy PDFAccessibility --app "python3 app.py" --require-approval never
else
  set -- "$@" --app "python3 app.py"
fi

echo "Running: cdk $*"
exec cdk "$@"
