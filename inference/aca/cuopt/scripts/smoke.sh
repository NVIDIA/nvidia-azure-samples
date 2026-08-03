#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

set -euo pipefail
base_url="${1:-http://localhost:8000}"
curl --fail --silent --show-error "${base_url}/api/health"
echo
curl --fail --silent --show-error "${base_url}/api/scenarios/freshroute-phoenix" | \
  python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["title"], d["morning_result"]["metrics"]["assigned_orders"], "orders")'
