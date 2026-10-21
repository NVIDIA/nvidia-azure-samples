# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Keep the same MCP client authenticated across projected token replacement."""
import builtins
import importlib.util
import json
import os
from pathlib import Path
import ssl
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("token_rotation_services", ROOT / "apps/lab_services.py")
services = importlib.util.module_from_spec(spec)
spec.loader.exec_module(services)
SERVICE_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"


class KubernetesTokenRotationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "namespace").write_text("ignite-lab\n")
        self.rotate("first")
        (self.root / "token").symlink_to("..data/token")
        self.received = []
        self.accepted = "first"
        self.reject = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner.received.append((self.command, self.headers.get("Authorization")))
                allowed = self.headers.get("Authorization") == "Bearer " + owner.accepted
                status = 401 if owner.reject or not allowed else 200
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"status": status}).encode())

            do_PATCH = do_GET

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop_server)
        real_open = builtins.open

        def open_projected(path, *args, **kwargs):
            if isinstance(path, str) and path.startswith(SERVICE_DIR + "/"):
                path = self.root / path.removeprefix(SERVICE_DIR + "/")
            return real_open(path, *args, **kwargs)

        self.context = ssl.create_default_context()
        self.enterContext(patch.object(services, "open", side_effect=open_projected, create=True))
        self.enterContext(patch.object(services.ssl, "create_default_context", return_value=self.context))
        self.enterContext(patch.dict(os.environ, {"KUBERNETES_SERVICE_HOST": "127.0.0.1"}))
        self.client = services.KubernetesApi()
        # The fixture uses synthetic credentials over loopback HTTP; production
        # continues to require the cluster HTTPS endpoint and mounted CA.
        self.client.base = "http://127.0.0.1:" + str(server.server_port)

    def rotate(self, value):
        generation = self.root / ("generation-" + value)
        generation.mkdir()
        (generation / "token").write_text(value + "\n")
        temporary = self.root / "..data-next"
        temporary.symlink_to(generation.name, target_is_directory=True)
        temporary.replace(self.root / "..data")

    def test_same_client_uses_rotated_symlink_for_next_api_request(self):
        self.assertEqual(self.client.request("/api/v1/configmaps")[0], 200)
        self.rotate("second")
        self.accepted = "second"
        self.assertEqual(self.client.request("/api/v1/configmaps")[0], 200)
        self.assertEqual(self.received, [("GET", "Bearer first"), ("GET", "Bearer second")])

    def test_empty_projected_token_stops_before_sending_cached_credentials(self):
        (self.root / "token").write_text("\n")
        with self.assertRaisesRegex(RuntimeError, "token is empty"):
            self.client.request("/api/v1/configmaps")
        self.assertEqual(self.received, [])

    def test_missing_projected_token_stops_before_sending_cached_credentials(self):
        (self.root / "token").unlink()
        with self.assertRaises(FileNotFoundError):
            self.client.request("/api/v1/configmaps")
        self.assertEqual(self.received, [])

    def test_unauthorized_patch_is_reported_without_retrying_the_mutation(self):
        self.reject = True
        status, body = self.client.request("/apis/apps/v1/deployments/service-api", method="PATCH",
                                           payload={"spec": {"replicas": 1}})
        self.assertEqual((status, body), (401, {"status": 401}))
        self.assertEqual(self.received, [("PATCH", "Bearer first")])


if __name__ == "__main__":
    unittest.main()
