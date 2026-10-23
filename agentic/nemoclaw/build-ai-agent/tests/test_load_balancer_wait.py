# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Exercise the bootstrap wait with real kubectl against a local Kubernetes API."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "infra/setup"))
import deploy_cluster


class ServiceApi(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def service(self, *, after_watch=False):
        mode = self.server.mode
        ready = mode == "ready" or (after_watch and mode in (
            "empty-ingress", "missing-ingress", "missing-status"))
        service = {"apiVersion": "v1", "kind": "Service", "metadata": {
            "name": "aks-ops-mcp", "namespace": "ignite-lab", "resourceVersion": "1"},
            "spec": {"type": "LoadBalancer", "ports": [{"port": 8443}]}}
        if ready:
            service["status"] = {"loadBalancer": {"ingress": [{"ip": "10.42.4.10"}]}}
        elif mode == "wrong-ip":
            service["status"] = {"loadBalancer": {"ingress": [{"ip": "10.42.4.11"}]}}
        elif mode == "missing-ingress":
            service["status"] = {"loadBalancer": {}}
        elif mode != "missing-status":
            service["status"] = {"loadBalancer": {"ingress": []}}
        return service

    def do_GET(self):
        url = urlparse(self.path)
        self.server.requests.append(self.path)
        status = 200
        if url.path == "/api":
            data = {"apiVersion": "v1", "kind": "APIVersions", "versions": ["v1"]}
        elif url.path == "/apis":
            data = {"apiVersion": "v1", "kind": "APIGroupList", "groups": []}
        elif url.path == "/api/v1":
            data = {"apiVersion": "v1", "kind": "APIResourceList", "groupVersion": "v1",
                "resources": [{"name": "services", "singularName": "service", "namespaced": True,
                    "kind": "Service", "verbs": ["get", "list", "watch"]}]}
        elif not url.path.startswith("/api/v1/namespaces/ignite-lab/services"):
            self.send_error(404)
            return
        elif self.server.mode == "forbidden":
            status = 403
            data = {"apiVersion": "v1", "kind": "Status", "status": "Failure",
                "reason": "Forbidden", "code": 403, "message": "fixture access denied"}
        elif url.path.endswith("/services/aks-ops-mcp"):
            data = self.service()
        elif parse_qs(url.query).get("watch") == ["true"]:
            time.sleep(0.03)
            data = {"type": "MODIFIED", "object": self.service(after_watch=True)}
        else:
            data = {"apiVersion": "v1", "kind": "ServiceList",
                "metadata": {"resourceVersion": "1"}, "items": [self.service()]}
        payload = (json.dumps(data) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # A bounded kubectl wait may close its watch at the timeout.


class LoadBalancerWaitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kubectl = shutil.which("kubectl")
        if not cls.kubectl:
            if os.environ.get("CI"):
                raise RuntimeError("CI must provide kubectl for the load-balancer regression")
            raise unittest.SkipTest("Real kubectl is required for the load-balancer regression")

    def run_wait(self, mode):
        # Extract the actual production command rather than duplicating its JSONPath.
        lines = [shlex.split(line) for line in deploy_cluster.cluster_setup_script().splitlines()
                 if line.startswith("kubectl ")]
        wait = next(line for line in lines if "wait" in line and "service/aks-ops-mcp" in line)
        wait = ["--timeout=2s" if arg.startswith("--timeout=") else arg for arg in wait]
        server = ThreadingHTTPServer(("127.0.0.1", 0), ServiceApi)
        server.mode, server.requests = mode, []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as cache:
                command = [self.kubectl, "--kubeconfig=/dev/null",
                    "--server=http://127.0.0.1:" + str(server.server_port),
                    "--cache-dir=" + cache, "--request-timeout=3s", *wait[1:]]
                result = subprocess.run(command, capture_output=True, text=True, timeout=10)
            return result, list(server.requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_empty_or_missing_ingress_waits_for_the_expected_ip(self):
        for mode in ("empty-ingress", "missing-ingress", "missing-status"):
            with self.subTest(mode=mode):
                result, requests = self.run_wait(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("condition met", result.stdout)
                self.assertTrue(any(parse_qs(urlparse(path).query).get("watch") == ["true"]
                                    for path in requests))

    def test_already_assigned_expected_ip_passes(self):
        result, _requests = self.run_wait("ready")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_pending_wrong_ip_and_forbidden_never_pass(self):
        for mode in ("pending", "wrong-ip", "forbidden"):
            with self.subTest(mode=mode):
                result, _requests = self.run_wait(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("condition met", result.stdout)
                self.assertNotIn("array index out of bounds", result.stderr)


if __name__ == "__main__":
    unittest.main()
