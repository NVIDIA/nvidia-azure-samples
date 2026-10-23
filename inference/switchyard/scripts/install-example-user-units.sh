#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
EXAMPLE_UNITS="${ROOT}/examples/systemd/user"

install -d -m 0700 "${HOME}/.config/switchyard"
install -d -m 0755 "${HOME}/.config/systemd/user"
install -d -m 0755 "${HOME}/.config/prometheus"
install -d -m 0755 "${HOME}/.local/bin"

install -m 0755 "${ROOT}/shim/switchyard_auth_proxy.py" "${HOME}/.local/bin/switchyard_auth_proxy.py"
install -m 0644 "${ROOT}/config/routes.toml" "${HOME}/.config/switchyard/routes.toml"
install -m 0644 "${ROOT}/config/prometheus.yml" "${HOME}/.config/prometheus/prometheus.yml"

for unit in "${EXAMPLE_UNITS}"/*.service; do
  install -m 0644 "${unit}" "${HOME}/.config/systemd/user/$(basename "${unit}")"
done

if [[ ! -f "${HOME}/.config/switchyard/env" ]]; then
  install -m 0600 "${ROOT}/config/env.example" "${HOME}/.config/switchyard/env"
  echo "Created ${HOME}/.config/switchyard/env; fill AZURE_FOUNDRY_API_KEY before starting."
fi

if [[ ! -f "${HOME}/.config/switchyard/cursor-proxy.env" ]]; then
  install -m 0600 "${ROOT}/config/cursor-proxy.env.example" "${HOME}/.config/switchyard/cursor-proxy.env"
  echo "Created ${HOME}/.config/switchyard/cursor-proxy.env; fill SWITCHYARD_CURSOR_API_KEY before starting."
fi

systemctl --user daemon-reload

if [[ "${1:-}" == "--start" ]]; then
  systemctl --user enable --now nemotron35-lightning-vllm.service
  systemctl --user enable --now switchyard-cursor.service
  systemctl --user enable --now switchyard-auth-proxy.service
  systemctl --user enable --now switchyard-ngrok.service
  systemctl --user enable --now prometheus-switchyard-agent.service
else
  systemctl --user enable nemotron35-lightning-vllm.service
  systemctl --user enable switchyard-cursor.service
  systemctl --user enable switchyard-auth-proxy.service
  systemctl --user enable switchyard-ngrok.service
  systemctl --user enable prometheus-switchyard-agent.service
  echo "Installed and enabled example units. Re-run with --start to start/restart them now."
fi
