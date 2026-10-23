// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// Exercise the same SDK version as the pinned OpenClaw release, without a model.
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
import path from 'node:path';

const root = process.env.IGNITE_MCP_SDK_ROOT;
const sdk = path.join(root, 'node_modules/@modelcontextprotocol/sdk/dist/esm');
const {Client} = await import(pathToFileURL(path.join(sdk, 'client/index.js')));
const {StreamableHTTPClientTransport} = await import(pathToFileURL(path.join(sdk, 'client/streamableHttp.js')));
const calls = [];
const errors = [];
const transport = new StreamableHTTPClientTransport(new URL(process.argv[2]), {
  requestInit: {headers: {Authorization: 'Bearer test-only-mcp-token'}},
  fetch: async (url, options) => {
    calls.push(options.method || 'GET');
    if (process.argv[3] === 'deny-get' && options.method === 'GET') {
      return new Response(JSON.stringify({error: 'policy_denied'}), {status: 403});
    }
    return fetch(url, options);
  },
});
const client = new Client({name: 'ignite-transport-test', version: '1.0.0'});
client.onerror = error => errors.push(error.name);
try {
  await client.connect(transport);
  const catalog = await client.listTools(undefined, {timeout: 30000});
  const names = catalog.tools.map(tool => tool.name);
  for (const required of ['verify_application', 'get_desired_config', 'get_runtime_config']) {
    assert(names.includes(required));
  }
  assert.equal(transport.sessionId, undefined);
  await transport.terminateSession();
  assert(!calls.includes('DELETE'));
  console.log(JSON.stringify({tools: names, calls, backgroundErrors: errors.length}));
} finally {
  await client.close();
}
