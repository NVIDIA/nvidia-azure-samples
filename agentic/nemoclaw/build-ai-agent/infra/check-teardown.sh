#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
# Read-only post-termination platform check, not a learner command. Skillable
# must remove its assigned main resource group; AKS deletion removes the node RG.
: "${IGNITE_LAB_RESOURCE_GROUP:?Assigned main resource group is required}"
: "${IGNITE_LAB_NODE_RESOURCE_GROUP:?Recorded template output is required}"
for lab_rg in "$IGNITE_LAB_RESOURCE_GROUP" "$IGNITE_LAB_NODE_RESOURCE_GROUP"; do
  if [[ "$(az group exists --name "$lab_rg" --only-show-errors)" != false ]]; then
    printf 'Cleanup incomplete: resource group %s still exists.\n' "$lab_rg" >&2
    exit 1
  fi
done
printf 'Main and AKS node resource groups are gone.\n'
