# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Run the real sandbox probe against controlled config and transport outcomes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


PROBE = Path(__file__).resolve().parents[1] / "infra/setup/probe_mcp.cjs"
NODE = shutil.which("node")
SECRET = "test-only-credential-must-not-appear"
DRIVER = r'''
const fs = require('node:fs');
const read = fs.readFileSync;
const mode = process.env.IGNITE_PROBE_TEST_MODE;
const secret = process.env.IGNITE_PROBE_TEST_SECRET;
fs.readFileSync = function(path, ...args) {
  if (path === '/sandbox/.openclaw/openclaw.json') return JSON.stringify({mcp: {servers: {
    'aks-ops': {url: 'https://private.example/mcp', headers: {
      Authorization: mode === 'literal-credential' ? secret : 'Bearer openshell:resolve:env:test_TOKEN'
    }}
  }}});
  return read.call(this, path, ...args);
};
const calls = [];
global.fetch = async (_url, options) => {
  const name = JSON.parse(options.body).params.name;
  calls.push(name);
  if (options.redirect !== 'manual') throw Error('Redirect following must be disabled');
  if (mode === 'socket') throw Object.assign(new Error(secret), {cause: {code: 'UND_ERR_SOCKET', message: secret}});
  if (mode === 'http') return {status: 401, text: async () => secret};
  if (mode === 'bad-json') return {status: 200, text: async () => secret};
  if (mode === 'rpc') return {status: 200, text: async () => JSON.stringify({error: {message: secret}})};
  if (name === 'verify_application') return {status: 200, text: async () => JSON.stringify({result: {
    content: [{type: 'text', text: JSON.stringify({status: mode === 'already-healthy' ? 200 : 503})}]
  }})};
  if (name === 'get_desired_config') return {status: 200, text: async () => JSON.stringify({result: {
    isError: mode === 'desired-tool-error',
    content: [{type: 'text', text: JSON.stringify({
      status: mode === 'desired-unauthorized' ? 401 : 200,
      data: {DEPENDENCY_URL: mode === 'desired-wrong-url' ? 'http://unexpected.example' : 'http://dependency-api:8081'}
    })}]
  }})};
  if (name === 'get_runtime_config') return {status: 200, text: async () => JSON.stringify({result: {
    content: [{type: 'text', text: JSON.stringify({
      status: mode === 'runtime-error' ? 503 : 200,
      effective_dependency_url: mode === 'runtime-wrong-url' ? 'http://unexpected.example' : 'http://dependency-api-invalid:8081'
    })}]
  }})};
  return {status: mode === 'mutation-allowed' ? 200 : 403, text: async () => secret};
};
process.on('exit', () => console.log('PROBE_CALLS=' + JSON.stringify(calls)));
require(process.argv[2]);
'''


@unittest.skipUnless(NODE, "Node.js is required to execute the sandbox readiness probe")
class SandboxMcpProbeTests(unittest.TestCase):
    def run_probe(self, mode):
        with tempfile.TemporaryDirectory() as folder:
            driver = Path(folder) / "driver.cjs"
            driver.write_text(DRIVER)
            result = subprocess.run([NODE, str(driver), str(PROBE)], capture_output=True, text=True,
                timeout=10, env={**os.environ, "IGNITE_PROBE_TEST_MODE": mode, "IGNITE_PROBE_TEST_SECRET": SECRET})
        output = result.stdout + result.stderr
        self.assertNotIn(SECRET, output)
        calls = json.loads(next(line.removeprefix("PROBE_CALLS=") for line in result.stdout.splitlines()
                                if line.startswith("PROBE_CALLS=")))
        return result, calls

    def test_success_requires_incident_and_both_mutation_denials(self):
        result, calls = self.run_probe("ready")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(calls, ["verify_application", "get_desired_config", "get_runtime_config",
                                 "restart_deployment", "scale_deployment"])
        self.assertIn("Managed MCP desired and runtime configuration verified.", result.stdout)
        self.assertIn("Managed MCP access and mutation denials verified.", result.stdout)

    def test_literal_credential_fails_before_any_network_request(self):
        result, calls = self.run_probe("literal-credential")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, [])
        self.assertIn("at configuration: MCP credential placeholder missing", result.stderr)

    def test_socket_error_reports_stage_and_code_without_arbitrary_exception_text(self):
        result, calls = self.run_probe("socket")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["verify_application"])
        self.assertIn("at application-health", result.stderr)
        self.assertIn("UND_ERR_SOCKET", result.stderr)

    def test_http_error_reports_status_without_response_body_or_mutations(self):
        result, calls = self.run_probe("http")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["verify_application"])
        self.assertIn("HTTP 401", result.stderr)

    def test_invalid_json_and_rpc_errors_stop_before_mutations(self):
        for mode in ("bad-json", "rpc"):
            with self.subTest(mode=mode):
                result, calls = self.run_probe(mode)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(calls, ["verify_application"])
                self.assertIn("at application-health", result.stderr)

    def test_previously_repaired_incident_does_not_test_mutations(self):
        result, calls = self.run_probe("already-healthy")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["verify_application"])
        self.assertIn("Expected the initial unhealthy incident", result.stderr)

    def test_missing_restart_denial_aborts_before_scaling(self):
        result, calls = self.run_probe("mutation-allowed")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(calls, ["verify_application", "get_desired_config", "get_runtime_config", "restart_deployment"])
        self.assertIn("at deny-restart_deployment: Expected policy denial HTTP 403", result.stderr)

    def test_failed_desired_read_stops_before_runtime_and_mutation_probes(self):
        for mode in ("desired-unauthorized", "desired-tool-error", "desired-wrong-url"):
            with self.subTest(mode=mode):
                result, calls = self.run_probe(mode)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(calls, ["verify_application", "get_desired_config"])
                self.assertIn("at desired-configuration", result.stderr)

    def test_failed_runtime_read_stops_before_mutation_probes(self):
        for mode in ("runtime-error", "runtime-wrong-url"):
            with self.subTest(mode=mode):
                result, calls = self.run_probe(mode)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(calls, ["verify_application", "get_desired_config", "get_runtime_config"])
                self.assertIn("at runtime-configuration", result.stderr)


if __name__ == "__main__":
    unittest.main()
