# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

FROM nvcr.io/nvidia/blueprint/aiq-agent:2.2.0

# Upstream image is distroless: no /bin/sh, so RUN must use exec form.

# AI-Q 2.2 includes knowledge-layer[all], including the native Azure AI Search
# backend. Only the Azure-specific workflow configuration is added here.
COPY config_web_azure.yml /app/configs/config_web_azure.yml
COPY sitecustomize.py /app/.venv/lib/python3.13/site-packages/sitecustomize.py

# NOTE: we deliberately do NOT override ENTRYPOINT or CMD. The upstream image
# orchestrates dask-scheduler + dask-worker + nat serve via /app/deploy/entrypoint.py
# and exports NAT_DASK_SCHEDULER_ADDRESS so `nat serve` connects to the
# managed cluster. Overriding the entrypoint forces NAT to spin up its own
# local Dask cluster, which traverses a different DB-init code path that
# breaks on the `?sslmode=require` URL form.
#
# Set CONFIG_FILE on the Container App env to point `nat serve` at this file:
#   CONFIG_FILE=/app/configs/config_web_azure.yml
