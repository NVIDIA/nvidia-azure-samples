# DGX Spark Switchyard Handoff Package

Start with `HANDOFF.md`.

This directory packages a DGX Spark Switchyard setup: auth shim, route config, optional systemd example units, Prometheus scrape config, and mini SWE-bench overlays. Credential files are represented as `.example` templates only.

```bash
cd /path/to/nvidia-azure-samples/inference/switchyard
./scripts/smoke-test.sh
```

To install the optional user-service example:

```bash
./scripts/install-example-user-units.sh
```
