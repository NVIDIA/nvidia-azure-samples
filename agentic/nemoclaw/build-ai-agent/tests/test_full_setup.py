# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Local contract/integration checks; they do not replace a fresh Azure rehearsal."""
import importlib.util
import hashlib
import json
import os
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "infra/setup"
sys.path.insert(0, str(SETUP))
from inference_broker import Broker, safe_rate_headers
from render_workloads import render, readiness_job
from deploy_cluster import invoke
import setup_vm


CONTEXT = {"labNamespace": "ignite-lab", "vmPrivateIp": "10.42.1.4", "mcpPrivateIp": "10.42.4.10",
           "upstreamInferenceEndpoint": "https://model.example/v1", "inferenceModel": "assigned-model",
           "inferenceAuthHeader": "Authorization", "resourceGroupName": "instance-rg", "aksClusterName": "instance-aks"}


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.manifest = render(CONTEXT, {"model-upstream": {"api-key": "test-only-upstream-secret"}}, ROOT)
        self.items = self.manifest["items"]

    def test_three_workloads_and_supported_application_modes(self):
        deployments = [item for item in self.items if item["kind"] == "Deployment"]
        self.assertEqual({item["metadata"]["name"] for item in deployments}, {"dependency-api", "service-api", "aks-ops-mcp"})
        for deployment in deployments:
            for container in deployment["spec"]["template"]["spec"]["containers"]:
                mode = next((env["value"] for env in container["env"] if env["name"] == "MODE"), None)
                if mode:
                    self.assertIn(mode, ("dependency", "service", "mcp"))
                self.assertNotIn("nvidia.com/gpu", container["resources"].get("limits", {}))
                self.assertIn("@sha256:", container["image"])

    def test_upstream_key_mounted_only_in_broker_not_mcp(self):
        pod = next(item for item in self.items if item["kind"] == "Deployment" and item["metadata"]["name"] == "aks-ops-mcp")["spec"]["template"]["spec"]
        mcp, broker = pod["containers"]
        self.assertNotIn("upstream", [v["name"] for v in mcp["volumeMounts"]])
        self.assertIn("upstream", [v["name"] for v in broker["volumeMounts"]])
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertIn("kube-token", [v["name"] for v in mcp["volumeMounts"]])
        self.assertNotIn("kube-token", [v["name"] for v in broker["volumeMounts"]])
        role = next(item for item in self.items if item["kind"] == "Role")
        for rule in role["rules"]:
            self.assertNotIn("secrets", rule["resources"])
            if "patch" in rule["verbs"]:
                self.assertEqual(rule["resourceNames"], ["service-api"])

    def test_private_lb_both_ports_select_same_pod_and_vm_only(self):
        service = next(item for item in self.items if item["kind"] == "Service" and item["metadata"]["name"] == "aks-ops-mcp")
        self.assertEqual(service["spec"]["loadBalancerSourceRanges"], ["10.42.1.4/32"])
        self.assertEqual({port["targetPort"] for port in service["spec"]["ports"]}, {8443, 8000})
        self.assertEqual(service["metadata"]["annotations"]["service.beta.kubernetes.io/azure-load-balancer-internal-subnet"], "lab-services")
        self.assertEqual(service["metadata"]["annotations"]["service.beta.kubernetes.io/azure-load-balancer-ipv4"], CONTEXT["mcpPrivateIp"])
        job = readiness_job(CONTEXT)
        self.assertFalse(job["spec"]["template"]["spec"]["automountServiceAccountToken"])

    def test_remote_nonzero_exit_never_reports_success(self):
        with patch("deploy_cluster.az", return_value={"exitCode": 1, "logs": "secret must not appear"}):
            with self.assertRaisesRegex(RuntimeError, "AKS bootstrap command failed"):
                invoke(CONTEXT, "false")
        with patch("deploy_cluster.az", return_value={"exitCode": 0}):
            self.assertEqual(invoke(CONTEXT, "true")["exitCode"], 0)

    def test_generated_template_matches_sources(self):
        subprocess.run([sys.executable, str(ROOT / "infra/build-template.py"), "--check"], check=True, capture_output=True)


class LearnerProvisioningTests(unittest.TestCase):
    """Exercise both provisioning modes without installing packages or calling Azure."""
    def run_provisioning(self, *, preinstall=None, fail_check=False, installer_ok=True,
                         agent_ok=True, agent_report=True):
        temporary = tempfile.TemporaryDirectory(prefix="ignite-provisioning-test-", dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        account = SimpleNamespace(pw_dir=str(root / "home/labuser"), pw_name="labuser", pw_uid=os.getuid(), pw_gid=os.getgid())
        context = {**CONTEXT, "deployLabSetup": True, "setupRevision": "learner-test",
                   "vmAdminUsername": "labuser", "inferenceEndpoint": "https://10.42.4.10:8000/v1"}
        if preinstall is not None:
            context["preinstallAgent"] = preinstall
        def vm_path(value):
            path = Path(value)
            if str(path).startswith(("/etc/", "/opt/", "/usr/", "/var/")):
                return root / str(path).lstrip("/")
            return path
        for folder in ("/etc/ignite-lab", "/opt/ignite-lab", "/usr/local/share/ca-certificates", "/var/log"):
            vm_path(folder).mkdir(parents=True)
        Path(account.pw_dir).mkdir(parents=True)
        vm_path("/etc/ignite-lab/context.json").write_text(json.dumps(context))
        # A failed re-provision must remove this stale successful status.
        vm_path("/etc/ignite-lab/ready.json").write_text('{"status":"ready"}')
        installer = b"#!/usr/bin/env bash\nexit 0\n"
        commands = []
        def run(command, **kwargs):
            commands.append(command)
            if command[0] == "curl":
                Path(command[-1]).write_bytes(installer if installer_ok else b"corrupt")
            if command[0] == "runuser" and "--non-interactive" in command[-1]:
                if agent_ok and agent_report:
                    report = Path(account.pw_dir) / ".config/ignite-lab/agent-ready.json"
                    report.write_text(json.dumps({"status": "ready", "setupRevision": context["setupRevision"],
                        "checks": {"agent": "ready", "mcpPolicy": "mutations-denied", "egress": "unapproved-destination-denied"}}))
                return subprocess.CompletedProcess(command, 0 if agent_ok else 1)
            return subprocess.CompletedProcess(command, 0)
        error = None
        with ExitStack() as stack:
            stack.enter_context(patch("setup_vm.Path", side_effect=vm_path))
            stack.enter_context(patch("setup_vm.os.geteuid", return_value=0))
            stack.enter_context(patch("setup_vm.os.umask"))
            stack.enter_context(patch("setup_vm.os.chown"))
            stack.enter_context(patch("setup_vm.pwd.getpwnam", return_value=account))
            stack.enter_context(patch("setup_vm.secret", side_effect=lambda _c, n: "instance-only-" + n))
            stack.enter_context(patch("setup_vm.subprocess.run", side_effect=run))
            stack.enter_context(patch("setup_vm.INSTALLER_SHA256", hashlib.sha256(installer).hexdigest()))
            stack.enter_context(patch("setup_vm.check", side_effect=RuntimeError("Cluster check failed") if fail_check else None,
                return_value={"mcp": "ready", "rbac": "isolated", "incident": "seeded", "inference": "ready"}))
            try:
                setup_vm.main()
            except RuntimeError as caught:
                error = caught
        status_path = vm_path("/etc/ignite-lab/ready.json")
        status = json.loads(status_path.read_text()) if status_path.exists() else None
        return root, account, context, commands, status, error

    def test_default_stages_working_infrastructure_without_installing_agent(self):
        root, account, context, commands, status, error = self.run_provisioning()
        self.assertIsNone(error)
        self.assertEqual(status["status"], "infrastructure-ready")
        self.assertEqual(status["phase"], "infrastructure")
        self.assertEqual(status["checks"]["inference"], "ready")
        self.assertEqual(status["checks"]["agent"], "awaiting-learner-install")
        self.assertNotIn("mcpPolicy", status["checks"])
        self.assertNotIn("egress", status["checks"])
        self.assertFalse(any("--non-interactive" in command[-1] for command in commands))
        config = Path(account.pw_dir) / ".config/ignite-lab"
        self.assertEqual(config.stat().st_mode & 0o777, 0o700)
        settings = config / "environment.sh"
        self.assertEqual(settings.stat().st_mode & 0o777, 0o600)
        self.assertIn("instance-only-broker-token", settings.read_text())
        self.assertNotIn("inferenceApiKey", settings.read_text())
        self.assertFalse((config / "agent-ready.json").exists())
        self.assertTrue((root / "opt/ignite-lab/install-nemoclaw-pinned.sh").exists())
        self.assertEqual((Path(account.pw_dir) / ".local").stat().st_mode & 0o777, 0o700)

    def test_preinstall_checks_agent_before_marking_full_readiness(self):
        _, _, _, commands, status, error = self.run_provisioning(preinstall=True)
        self.assertIsNone(error)
        self.assertEqual(status["status"], "ready")
        self.assertEqual(status["phase"], "agent")
        self.assertEqual(status["checks"]["mcpPolicy"], "mutations-denied")
        install_commands = [c for c in commands if "--non-interactive" in c[-1]]
        self.assertEqual(len(install_commands), 1)
        self.assertIn("--yes-i-accept-third-party-software", install_commands[0][-1])
        self.assertIn("setup_user.sh", install_commands[0][-1])

    def test_platform_failure_and_bad_installer_remove_stale_readiness(self):
        for changes in ({"fail_check": True}, {"installer_ok": False}):
            with self.subTest(changes=changes):
                _, _, _, commands, status, error = self.run_provisioning(**changes)
                self.assertIsNotNone(error)
                self.assertIsNone(status)
                self.assertFalse(any("--non-interactive" in c[-1] for c in commands))

    def test_preinstall_failure_or_missing_agent_report_never_claims_ready(self):
        for changes in ({"agent_ok": False}, {"agent_report": False}):
            with self.subTest(changes=changes):
                _, _, _, _, status, error = self.run_provisioning(preinstall=True, **changes)
                self.assertIsNotNone(error)
                self.assertIsNone(status)

    def test_settings_round_trip_without_executing_shell_metacharacters(self):
        with tempfile.TemporaryDirectory(prefix="ignite-settings-test-") as folder:
            config = Path(folder)
            marker = config / "injected"
            malicious = "model'; touch " + str(marker) + "; # $(touch " + str(marker) + ") `touch " + str(marker) + "`\nmodel"
            account = SimpleNamespace(pw_dir=folder, pw_uid=os.getuid(), pw_gid=os.getgid())
            with patch("setup_vm.os.chown"):
                setup_vm.stage_environment(config, account, {"NEMOCLAW_MODEL": malicious, "COMPATIBLE_API_KEY": "test-only-token"})
            result = subprocess.run(["bash", "-c", 'source "$1"\nexec "$2" -c \'import json,os; print(json.dumps(os.environ["NEMOCLAW_MODEL"]))\'',
                "settings-test", str(config / "environment.sh"), sys.executable], capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(result.stdout), malicious)
            self.assertFalse(marker.exists())


class Upstream(BaseHTTPRequestHandler):
    received = []
    def log_message(self, *_args):
        pass
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.received.append((self.path, self.headers.get("Authorization"), body, self.headers.get("api-key")))
        content = body["messages"][0]["content"]
        if content.startswith("limited-"):
            self.send_response(429)
            self.send_header("Retry-After", "test-only-upstream-secret" if content == "limited-unsafe" else "60")
            self.send_header("X-Ratelimit-Limit-Tokens", "test-only-upstream-secret" if content == "limited-unsafe" else "100000")
            self.send_header("X-Ratelimit-Limit-Tokens-Prompt", "50000")
            self.send_header("X-Provider-Private", "test-only-upstream-secret")
            self.end_headers()
            message = {"limited-tokens": "Token rate limit exceeded test-only-upstream-secret",
                       "limited-requests": "Requests per minute exceeded test-only-upstream-secret"}.get(content, "test-only-upstream-secret")
            self.wfile.write(json.dumps({"error": {"message": message}}).encode())
            return
        if body["messages"][0]["content"] == "error":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"test-only-upstream-secret")
            return
        if body["messages"][0]["content"] == "redirect":
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.1:" + str(self.server.server_port) + "/collect")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("X-Ratelimit-Limit-Tokens-Prompt", "50000")
        self.send_header("X-Provider-Private", "test-only-upstream-secret")
        self.end_headers()
        if body.get("stream"):
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"ready"}}]}\n\ndata: [DONE]\n\n')
        else:
            self.wfile.write(b'{"choices":[{"message":{"content":"ready"}}]}')


class BrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=localhost",
                        "-addext", "subjectAltName=DNS:localhost", "-keyout", str(root / "key"), "-out", str(root / "cert")],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(root / "cert", root / "key")
        cls.upstream.socket = tls.wrap_socket(cls.upstream.socket, server_side=True)
        cls.broker = Broker(("127.0.0.1", 0), endpoint="https://localhost:" + str(cls.upstream.server_port) + "/v1",
                            model="assigned-model", upstream_key="test-only-upstream-secret", client_key="per-lab-token")
        cls.trust = ssl.create_default_context(cafile=str(root / "cert"))
        # Trust only the local test CA for the broker's upstream request.
        original = urllib.request.build_opener
        cls.patch = patch("inference_broker.urllib.request.build_opener", side_effect=lambda *handlers: original(
            *handlers, urllib.request.HTTPSHandler(context=cls.trust)))
        cls.patch.start()
        for server in (cls.upstream, cls.broker):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.patch.stop()
        for server in (cls.broker, cls.upstream):
            server.shutdown()
            server.server_close()
        cls.temp.cleanup()

    def request(self, token="per-lab-token", include_headers=False, **updates):
        body = dict(model="assigned-model", messages=[{"role": "user", "content": "ready"}], max_tokens=64)
        body.update(updates)
        req = urllib.request.Request("http://127.0.0.1:" + str(self.broker.server_port) + "/v1/chat/completions",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                value = (response.status, response.read())
                return (*value, response.headers) if include_headers else value
        except urllib.error.HTTPError as error:
            with error:
                value = (error.code, error.read())
                return (*value, error.headers) if include_headers else value

    def test_real_response_and_upstream_key_not_client_token(self):
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertIn(b"ready", body)
        self.assertEqual(Upstream.received[-1][1], "Bearer test-only-upstream-secret")
        self.assertEqual(Upstream.received[-1][0], "/v1/chat/completions")

    def test_foundry_v1_path_and_api_key_header_preserve_client_token_isolation(self):
        endpoint, auth_header = self.broker.endpoint, self.broker.auth_header
        try:
            self.broker.endpoint = endpoint.removesuffix('/v1') + '/openai/v1'
            self.broker.auth_header = 'api-key'
            status, body = self.request()
            self.assertEqual(status, 200)
            self.assertIn(b'ready', body)
            self.assertEqual(Upstream.received[-1][0], '/openai/v1/chat/completions')
            self.assertIsNone(Upstream.received[-1][1])
            self.assertEqual(Upstream.received[-1][3], 'test-only-upstream-secret')
            self.assertNotIn(b'test-only-upstream-secret', body)
        finally:
            self.broker.endpoint, self.broker.auth_header = endpoint, auth_header

    def test_invalid_auth_and_model_do_not_reach_provider(self):
        self.assertEqual(self.request(token="wrong")[0], 401)
        self.assertEqual(self.request(model="another-model")[0], 400)

    def test_streaming_and_upstream_error_redaction(self):
        status, body = self.request(stream=True)
        self.assertEqual(status, 200)
        self.assertIn(b"data: [DONE]", body)
        status, body = self.request(messages=[{"role": "user", "content": "error"}])
        self.assertEqual(status, 502)
        self.assertNotIn(b"test-only-upstream-secret", body)

    def test_upstream_429_exposes_only_fixed_category_and_safe_quota_headers(self):
        for content, dimension in (("limited-tokens", "tokens"), ("limited-requests", "requests"), ("limited-unknown", "unspecified")):
            with self.subTest(dimension=dimension):
                status, body, headers = self.request(include_headers=True, messages=[{"role": "user", "content": content}])
                self.assertEqual(status, 429)
                result = json.loads(body)
                self.assertEqual(result["diagnostics"]["source"], "upstream")
                self.assertEqual(result["diagnostics"]["reportedLimit"], dimension)
                self.assertIn("(" + dimension + ")", result["error"])
                self.assertEqual(result["diagnostics"]["retryAfterSeconds"], 60)
                self.assertEqual(headers.get("Retry-After"), "60")
                self.assertEqual(headers.get("X-Ratelimit-Limit-Tokens-Prompt"), "50000")
                self.assertIsNone(headers.get("X-Provider-Private"))
                self.assertNotIn(b"test-only-upstream-secret", body)

    def test_invalid_provider_header_values_do_not_leave_broker(self):
        status, body, headers = self.request(include_headers=True, messages=[{"role": "user", "content": "limited-unsafe"}])
        self.assertEqual(status, 429)
        self.assertIsNone(headers.get("Retry-After"))
        self.assertIsNone(headers.get("X-Ratelimit-Limit-Tokens"))
        self.assertNotIn(b"test-only-upstream-secret", body)

    def test_successful_json_and_sse_preserve_numeric_quota_headers_only(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                status, body, headers = self.request(stream=stream, include_headers=True)
                self.assertEqual(status, 200)
                self.assertIn(b"ready", body)
                self.assertEqual(headers.get("X-Ratelimit-Limit-Tokens-Prompt"), "50000")
                self.assertIsNone(headers.get("X-Provider-Private"))

    def test_local_concurrency_limit_has_separate_message_and_retry_hint(self):
        acquired = 0
        try:
            # A previous response may reach its client just before its handler
            # releases the slot. Wait until we own all four, not only the slots
            # that happen to be free during a nonblocking scan.
            for _ in range(4):
                self.assertTrue(self.broker.slots.acquire(timeout=5))
                acquired += 1
            status, body, headers = self.request(include_headers=True)
            self.assertEqual(status, 429)
            self.assertEqual(json.loads(body)["error"], "Too many concurrent requests")
            self.assertEqual(headers.get("Retry-After"), "1")
        finally:
            for _ in range(acquired):
                self.broker.slots.release()

    def test_output_cap_and_request_budget(self):
        self.assertEqual(self.request(max_tokens=4097)[0], 400)
        with self.broker.lock:
            previous = self.broker.requests_used
            self.broker.requests_used = self.broker.max_requests
        try:
            self.assertEqual(self.request()[0], 429)
        finally:
            self.broker.requests_used = previous

    def test_redirect_never_forwards_credential(self):
        before = len(Upstream.received)
        self.assertEqual(self.request(messages=[{"role": "user", "content": "redirect"}])[0], 502)
        self.assertEqual(len(Upstream.received), before + 1)


class RateHeaderTests(unittest.TestCase):
    def test_retry_after_normalizes_seconds_and_azure_milliseconds(self):
        self.assertEqual(safe_rate_headers({"Retry-After": "1.2"}), {"Retry-After": "2"})
        self.assertEqual(safe_rate_headers({"retry-after-ms": "1500"}), {"Retry-After": "2"})

    def test_retry_after_accepts_http_date_without_forwarding_arbitrary_text(self):
        self.assertEqual(safe_rate_headers({"Retry-After": "Wed, 01 Jan 2020 00:00:00 GMT"}), {"Retry-After": "0"})
        for value in ("test-only-secret", "1000000000", "99999999", "1\r\nAuthorization: secret"):
            self.assertNotIn("Retry-After", safe_rate_headers({"Retry-After": value}))

    def test_quota_header_values_and_names_are_strictly_bounded(self):
        for value in ("secret", "-1", "1234567890123", "10\r\nX-Secret: secret"):
            self.assertEqual(safe_rate_headers({"x-ratelimit-limit-tokens": value}), {})
        self.assertEqual(safe_rate_headers({"x-ratelimit-limit-tokens": "100000", "Authorization": "secret"}),
                         {"x-ratelimit-limit-tokens": "100000"})


if __name__ == "__main__":
    unittest.main()
