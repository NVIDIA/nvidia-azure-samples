# Manifest

No credential-bearing files are included.

```text
HANDOFF.md                                      Quick operational handoff
README.md                                      Package entrypoint
shim/switchyard_auth_proxy.py                  Cursor bearer-auth and payload compatibility shim
shim/requirements.txt                          Python dependency for the shim
config/routes.toml                             Default Switchyard routes and upstream endpoints
config/env.example                             Secret template for Switchyard upstream clients
config/cursor-proxy.env.example                Secret template for the auth shim
config/prometheus.yml                          Prometheus scrape config with optional remote_write template
examples/systemd/user/nemotron35-lightning-vllm.service Example local Nemotron vLLM service
examples/systemd/user/switchyard-cursor.service         Example Switchyard router service on port 4000
examples/systemd/user/switchyard-auth-proxy.service     Example auth shim service on port 4100
examples/systemd/user/switchyard-ngrok.service          Example ngrok tunnel service
examples/systemd/user/prometheus-switchyard-agent.service Example Prometheus agent service
benchmarks/switchyard-mini.yaml                mini SWE-bench smart route overlay
benchmarks/switchyard-mini-base.yaml           mini SWE-bench Azure passthrough overlay
scripts/install-example-user-units.sh          Installs optional example user-service units and supporting files
scripts/smoke-test.sh                          Local health and model-list checks
```
