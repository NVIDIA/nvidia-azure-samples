#!/usr/bin/env bash
set -euo pipefail

echo "Checking auth shim health..."
curl -fsS http://127.0.0.1:4100/health
echo

echo "Checking Switchyard model list..."
curl -fsS http://127.0.0.1:4000/v1/models
echo

if [[ -f "${HOME}/.config/switchyard/cursor-proxy.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  . "${HOME}/.config/switchyard/cursor-proxy.env"
  set +a
fi

if [[ -n "${SWITCHYARD_CURSOR_API_KEY:-}" && "${SWITCHYARD_CURSOR_API_KEY}" != "REPLACE_WITH_LONG_RANDOM_TOKEN" ]]; then
  echo "Checking authenticated shim model list..."
  curl -fsS \
    -H "Authorization: Bearer ${SWITCHYARD_CURSOR_API_KEY}" \
    http://127.0.0.1:4100/v1/models
  echo
else
  echo "Skipping authenticated shim check because SWITCHYARD_CURSOR_API_KEY is not set."
fi

echo "Checking ngrok tunnel metadata if local inspector is enabled..."
curl -fsS http://127.0.0.1:4040/api/tunnels || true
echo
