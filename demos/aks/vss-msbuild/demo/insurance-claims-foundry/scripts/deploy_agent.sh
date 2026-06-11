#!/usr/bin/env bash
set -euo pipefail

agent="${1:-insurance-claims-triage-vss}"

case "$agent" in
  insurance-claims-triage|insurance-claims-triage-vss) ;;
  *)
    echo "Usage: $0 [insurance-claims-triage|insurance-claims-triage-vss]" >&2
    exit 2
    ;;
esac

cd "$(dirname "$0")/.."
azd deploy "$agent"
