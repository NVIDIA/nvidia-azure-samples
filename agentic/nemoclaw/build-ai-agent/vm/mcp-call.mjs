// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

const fs = require("node:fs");

const method = process.argv[2] || "tools/list";
const toolName = process.argv[3] || "";
const toolArguments = JSON.parse(process.argv[4] || "{}");
const openClawConfig = JSON.parse(
  fs.readFileSync("/sandbox/.openclaw/openclaw.json", "utf8"),
);
const mcpUrl = openClawConfig?.mcp?.servers?.["aks-ops"]?.url;
const authorization = openClawConfig?.mcp?.servers?.["aks-ops"]?.headers?.Authorization;
if (typeof mcpUrl !== "string" || !mcpUrl.startsWith("https://")) {
  throw new Error("managed MCP HTTPS URL is missing");
}
if (typeof authorization !== "string" || !authorization.includes("openshell:resolve:")) {
  throw new Error("managed MCP credential placeholder is missing");
}

const params = method === "tools/call"
  ? { name: toolName, arguments: toolArguments }
  : {};
const response = await fetch(mcpUrl, {
  method: "POST",
  headers: {
    "Authorization": authorization,
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
  },
  body: JSON.stringify({
    jsonrpc: "2.0",
    id: `${Date.now()}`,
    method,
    params,
  }),
});

const text = await response.text();
let body;
try {
  body = JSON.parse(text);
} catch {
  body = { text: text.slice(0, 2048) };
}

console.log(JSON.stringify({
  httpStatus: response.status,
  method,
  toolName: toolName || undefined,
  body,
}, null, 2));

if (!response.ok || body?.error) {
  process.exitCode = 2;
}
