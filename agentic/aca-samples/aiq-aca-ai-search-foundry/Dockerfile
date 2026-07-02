FROM nvcr.io/nvidia/blueprint/aiq-agent:2.1.0

# Upstream image is distroless: no /bin/sh, so RUN must use exec form.

# Local source install — the adapter lives in ./aiq-azure-ai-search/ alongside
# this Dockerfile and is shipped as part of the `az acr build` context. Switch
# to `pip install git+https://...` once the package is published.
COPY ./aiq-azure-ai-search /tmp/adapter
RUN ["/app/.venv/bin/pip", "install", "--no-cache-dir", "/tmp/adapter"]

# Custom AI-Q config that selects _type: azure_ai_search_retrieval
COPY config_web_azure.yml /app/configs/config_web_azure.yml

# NOTE: we deliberately do NOT override ENTRYPOINT or CMD. The upstream image
# orchestrates dask-scheduler + dask-worker + nat serve via /app/deploy/entrypoint.py
# and exports NAT_DASK_SCHEDULER_ADDRESS so `nat serve` connects to the
# managed cluster. Overriding the entrypoint forces NAT to spin up its own
# local Dask cluster, which traverses a different DB-init code path that
# breaks on the `?sslmode=require` URL form.
#
# The adapter is auto-discovered at NAT startup via the `nat.plugins` entry
# point declared in aiq-azure-ai-search/pyproject.toml, so no manual import
# is needed here.
#
# Set CONFIG_FILE on the Container App env to point `nat serve` at this file:
#   CONFIG_FILE=/app/configs/config_web_azure.yml
