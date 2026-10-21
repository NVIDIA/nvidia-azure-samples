// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

const fs = require("node:fs");
const config = JSON.parse(fs.readFileSync("/sandbox/.openclaw/openclaw.json", "utf8"));

function sanitize(value) {
  if (Array.isArray(value)) return value.map(sanitize);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, sanitize(item)]));
  }
  if (typeof value !== "string") return value;
  if (
    value.includes("openshell:")
    || value.includes("AKS_OPS_MCP_TOKEN")
  ) return value;
  if (/authorization|bearer|token|secret|key/i.test(value)) return "<redacted>";
  return value.length > 256 ? `${value.slice(0, 256)}...` : value;
}

console.log(JSON.stringify(sanitize(config?.mcp?.servers?.["aks-ops"]), null, 2));
