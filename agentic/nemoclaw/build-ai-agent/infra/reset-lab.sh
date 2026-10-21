#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
# Author/platform only. Rerun the full template with a NEW setupRevision and
# the original connection selection. This resets the incident and agent together.
: "${IGNITE_LAB_RESOURCE_GROUP:?Set the assigned lab resource group}"
: "${IGNITE_LAB_SETUP_REVISION:?Set a new nonsecret revision}"
: "${IGNITE_LAB_INSTANCE_ID:?Preserve the original Skillable lab instance ID}"
: "${IGNITE_LAB_ALLOW_AZURE_CLOUD_SSH:?Preserve the original true/false access setting}"
: "${IGNITE_LAB_VM_PASSWORD:?Supply the original VM credential through the platform}"
# Automatic Foundry discovery retrieves the key again. For an explicit endpoint,
# require its original secure credential rather than changing connection modes.
if [[ -n "${IGNITE_LAB_INFERENCE_ENDPOINT:-}" ]]; then
  : "${IGNITE_LAB_INFERENCE_API_KEY:?Supply the original secure model credential}"
fi
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
az deployment group create --resource-group "$IGNITE_LAB_RESOURCE_GROUP" \
  --name ignite-lab-reset --parameters "$script_dir/main.bicepparam" \
  --only-show-errors --output none
