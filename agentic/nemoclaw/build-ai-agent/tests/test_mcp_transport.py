# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Verify native MCP lifecycle behavior, not just hand-written tools/call POSTs."""
import http.client
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("transport_lab_services", ROOT / "apps/lab_services.py")
services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(services)
sys.path.insert(0, str(ROOT / "infra/setup"))
from check_mcp_catalog import check_catalog


class FixtureHandler(services.McpHandler):
    token = "test-only-mcp-token"

    def log_message(self, *_args):
        pass


class McpTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def request(self, method="POST", body=None, *, authorized=True, target="/mcp"):
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + FixtureHandler.token
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        self.addCleanup(connection.close)
        connection.request(method, target, body=json.dumps(body) if body is not None else None, headers=headers)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def test_initialize_and_tool_listing_do_not_advertise_fake_sessions(self):
        for method in ("initialize", "tools/list", "ping", "unknown_method"):
            with self.subTest(method=method):
                status, headers, raw = self.request(body={"jsonrpc": "2.0", "id": 1, "method": method})
                self.assertEqual(status, 200)
                self.assertNotIn("mcp-session-id", {key.lower() for key in headers})
                body = json.loads(raw)
                if method == "tools/list":
                    self.assertIn("verify_application", [tool["name"] for tool in body["result"]["tools"]])
                elif method == "unknown_method":
                    self.assertEqual(body["error"]["code"], -32601)

    def test_initialized_notification_is_an_empty_accepted_response(self):
        status, headers, raw = self.request(body={"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual((status, raw), (202, b""))
        self.assertEqual(headers["Content-Length"], "0")

    def test_unsupported_event_stream_and_session_cleanup_return_405(self):
        for method in ("GET", "DELETE"):
            with self.subTest(method=method):
                status, headers, _raw = self.request(method)
                self.assertEqual(status, 405)
                self.assertEqual(headers["Allow"], "POST")
                self.assertNotEqual(headers["Content-Type"], "text/event-stream")

    def test_authentication_covers_every_transport_operation(self):
        for method in ("GET", "DELETE", "POST"):
            with self.subTest(method=method):
                status, _headers, _raw = self.request(method, {"method": "tools/list"}, authorized=False)
                self.assertEqual(status, 401)

    def test_backend_failures_are_tool_errors_and_expected_incident_is_a_result(self):
        for tool, output, expected_error in (
            ("get_desired_config", {"status": 401, "data": {}}, True),
            ("get_runtime_config", {"status": 500}, True),
            ("restart_deployment", {"status": 403}, True),
            ("verify_application", {"status": 503, "healthy": False}, False),
            ("verify_application", {"status": 200, "healthy": True}, False),
            ("rbac_self_test", {"authorized_status": 200, "outside_status": 403,
                               "secrets_status": 403, "isolation_enforced": True}, False),
        ):
            with self.subTest(tool=tool, output=output):
                with patch.object(FixtureHandler, "_tool", return_value=output):
                    status, _, raw = self.request(body={"jsonrpc": "2.0", "id": 1,
                        "method": "tools/call", "params": {"name": tool, "arguments": {}}})
                self.assertEqual(status, 200)
                result = json.loads(raw)["result"]
                self.assertEqual(result["isError"], expected_error)
                self.assertEqual(json.loads(result["content"][0]["text"]), output)

    @unittest.skipUnless(os.environ.get("IGNITE_MCP_SDK_ROOT") and shutil.which("node"), "Pinned MCP SDK test runtime is not configured")
    def test_native_sdk_can_discover_and_close_without_session_delete(self):
        url = "http://%s:%s/mcp" % self.server.server_address
        for mode in ("server-405", "deny-get"):
            with self.subTest(mode=mode):
                result = subprocess.run(["node", str(ROOT / "tests/mcp_native_client.mjs"), url, mode],
                                        capture_output=True, text=True, timeout=15, check=True)
                report = json.loads(result.stdout)
                self.assertNotIn("DELETE", report["calls"])
                self.assertIn("get_runtime_config", report["tools"])


class NativeCatalogReadinessTests(unittest.TestCase):
    def catalog(self):
        return {"servers": {"aks-ops": {"tools": 3}}, "tools": [
            "mcp_aks_ops_verify_application", "mcp_aks_ops_get_desired_config", "mcp_aks_ops_get_runtime_config"
        ], "diagnostics": []}

    def test_incident_tools_must_be_discovered_before_readiness(self):
        check_catalog(self.catalog())
        for change in ("missing-server", "missing-tool", "diagnostic", "bad-count"):
            with self.subTest(change=change):
                report = self.catalog()
                if change == "missing-server":
                    report["servers"] = {}
                elif change == "missing-tool":
                    report["tools"].pop()
                elif change == "diagnostic":
                    report["diagnostics"] = [{"serverName": "aks-ops", "message": "remote-secret"}]
                else:
                    report["servers"]["aks-ops"]["tools"] = True
                with self.assertRaises(ValueError):
                    check_catalog(report)

    def test_cli_does_not_echo_remote_failure_details(self):
        with tempfile.TemporaryDirectory() as folder:
            report = self.catalog()
            report["diagnostics"] = [{"message": "test-only-remote-secret"}]
            target = Path(folder) / "catalog.json"
            target.write_text(json.dumps(report))
            result = subprocess.run([sys.executable, str(ROOT / "infra/setup/check_mcp_catalog.py"), str(target)],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("test-only-remote-secret", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
