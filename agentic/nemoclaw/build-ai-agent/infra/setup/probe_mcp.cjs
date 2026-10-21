// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

// Probe the OpenShell credential placeholder and both initial mutation denials.
const fs = require('node:fs');
class ReadinessFailure extends Error {}
let stage = 'configuration';
(async () => {
  const config = JSON.parse(fs.readFileSync('/sandbox/.openclaw/openclaw.json', 'utf8'));
  const server = config.mcp?.servers?.['aks-ops'];
  if (!server?.headers?.Authorization?.includes('openshell:resolve:')) throw new ReadinessFailure('MCP credential placeholder missing');
  async function call(name, args) {
    const response = await fetch(server.url, {method: 'POST', headers: {
      'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
      Authorization: server.headers.Authorization}, body: JSON.stringify({jsonrpc: '2.0', id: 1,
        method: 'tools/call', params: {name, arguments: args}}), redirect: 'manual', signal: AbortSignal.timeout(30000)});
    const body = await response.text();
    return {status: response.status, body};
  }
  async function read(name) {
    const response = await call(name, {});
    if (response.status !== 200) throw new ReadinessFailure(`Managed MCP returned HTTP ${response.status}`);
    const body = JSON.parse(response.body);
    if (body.error || body.result?.isError) throw new ReadinessFailure('Managed MCP tool failed');
    return JSON.parse(body.result.content[0].text);
  }
  stage = 'application-health';
  const detail = await read('verify_application');
  if (detail.status !== 503) throw new ReadinessFailure('Expected the initial unhealthy incident (HTTP 503)');
  console.log('Managed MCP access and initial application HTTP 503 verified.');
  stage = 'desired-configuration';
  const desired = await read('get_desired_config');
  if (desired.status !== 200) throw new ReadinessFailure('Desired configuration read did not return HTTP 200');
  if (desired.data?.DEPENDENCY_URL !== 'http://dependency-api:8081') throw new ReadinessFailure('Desired dependency URL is incorrect');
  stage = 'runtime-configuration';
  const runtime = await read('get_runtime_config');
  if (runtime.status !== 200) throw new ReadinessFailure('Runtime configuration read did not return HTTP 200');
  if (runtime.effective_dependency_url !== 'http://dependency-api-invalid:8081') throw new ReadinessFailure('Incident runtime dependency URL is incorrect');
  console.log('Managed MCP desired and runtime configuration verified.');
  // Valid mutations would change state if policy were absent. A 403 must occur
  // before forwarding; failure aborts provisioning and never marks the lab ready.
  for (const [name, args] of [
    ['restart_deployment', {deployment: 'service-api', reason: 'provisioning-denial-probe'}],
    ['scale_deployment', {deployment: 'service-api', replicas: 2}]
  ]) {
    stage = `deny-${name}`;
    const result = await call(name, args);
    if (result.status !== 403) throw new ReadinessFailure(`Expected policy denial HTTP 403; received HTTP ${result.status}`);
  }
  console.log('Managed MCP access and mutation denials verified.');
})().catch(error => {
  // Never print headers, endpoint response bodies or arbitrary exception text.
  const detail = error instanceof ReadinessFailure ? `: ${error.message}` : '';
  console.error(`Sandbox MCP readiness failed at ${stage}${detail}`);
  const code = error?.cause?.code || error?.code;
  if (typeof code === 'string' && /^(UND_ERR_[A-Z_]+|ERR_[A-Z_]+|E[A-Z]+|CERT_[A-Z_]+|UNABLE_TO_[A-Z_]+|DEPTH_ZERO_SELF_SIGNED_CERT|SELF_SIGNED_CERT_IN_CHAIN|ABORT_ERR)$/.test(code)) {
    console.error(`Network/runtime code: ${code}`);
  }
  process.exitCode = 1;
});
