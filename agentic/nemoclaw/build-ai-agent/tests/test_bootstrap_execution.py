# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Bound setup waits and report useful failures without exposing remote secrets."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/setup"))
import deploy_cluster


CONTEXT = {"resourceGroupName": "instance-rg", "aksClusterName": "instance-aks"}
SECRET = "test-only-remote-secret"


class BootstrapExecutionTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0
        self.addCleanup(patch.stopall)
        patch("deploy_cluster.time.monotonic", side_effect=lambda: self.clock).start()
        patch("deploy_cluster.time.sleep", side_effect=self.advance).start()
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))

    def advance(self, seconds):
        self.clock += seconds

    def test_cli_retries_share_deadline_and_redact_arguments_and_errors(self):
        timeouts = []
        def run(command, **kwargs):
            timeouts.append(kwargs["timeout"])
            self.assertIn(SECRET, command)
            self.advance(10)
            return SimpleNamespace(returncode=1, stdout=SECRET, stderr=SECRET)
        with patch("deploy_cluster.subprocess.run", side_effect=run) as cli:
            with self.assertRaisesRegex(RuntimeError, "Azure action timed out: keyvault secret set") as error:
                deploy_cluster.az("keyvault", "secret", "set", "--value", SECRET,
                                  attempts=20, timeout=60, retry_timeout=40)
        self.assertEqual(cli.call_count, 2)
        self.assertEqual(timeouts, [40, 10])
        self.assertEqual(self.clock, 40)
        self.assertNotIn(SECRET, self.output.getvalue() + str(error.exception))

    def test_cli_timeout_is_caught_and_has_no_remote_output(self):
        def run(command, **kwargs):
            self.assertEqual(kwargs["timeout"], 60)
            self.advance(60)
            raise deploy_cluster.subprocess.TimeoutExpired(command, 60, output=SECRET, stderr=SECRET)
        with patch("deploy_cluster.subprocess.run", side_effect=run):
            with self.assertRaisesRegex(RuntimeError, "timed out: aks command invoke") as error:
                deploy_cluster.az("aks", "command", "invoke")
        self.assertEqual(self.clock, 60)
        self.assertNotIn(SECRET, self.output.getvalue() + str(error.exception))

    def test_cli_success_returns_json_without_logging_response(self):
        response = SimpleNamespace(returncode=0, stdout=json.dumps({"key1": SECRET}), stderr="")
        with patch("deploy_cluster.subprocess.run", return_value=response):
            self.assertEqual(deploy_cluster.az("cognitiveservices", "account", "keys", "list"), {"key1": SECRET})
        self.assertNotIn(SECRET, self.output.getvalue())

    def test_remote_failure_is_not_retried_and_only_known_phase_is_reported(self):
        for marker, expected in [("readiness", " at readiness;"), ("", "failed;"),
                                 (SECRET, "failed;")]:
            with self.subTest(marker=marker), patch("deploy_cluster.az", return_value={
                    "exitCode": 1, "logs": SECRET + "\nIGNITE_SETUP_FAILURE phase=" + marker}) as cli:
                with self.assertRaises(RuntimeError) as error:
                    deploy_cluster.invoke(CONTEXT, "sh cluster-setup.sh", attempts=20)
                self.assertIn(expected, str(error.exception))
                self.assertNotIn(SECRET, str(error.exception) + self.output.getvalue())
                cli.assert_called_once()
                self.assertEqual(cli.call_args.kwargs["retry_timeout"], 1200)

    def test_authorization_retries_only_read_only_reviews_then_proceeds(self):
        with patch("deploy_cluster.az", side_effect=[{"exitCode": 1, "logs": SECRET},
                                                    {"exitCode": 0}]) as cli:
            deploy_cluster.wait_for_cluster_access(CONTEXT, timeout=30)
        self.assertEqual(self.clock, 10)
        self.assertEqual(cli.call_count, 2)
        for call in cli.call_args_list:
            command = call.args[call.args.index("--command") + 1]
            self.assertTrue(all(part.startswith("kubectl auth can-i create ") for part in command.split(" && ")))
            self.assertNotIn("--file", call.args)
            self.assertLessEqual(call.kwargs["timeout"], 30)
            self.assertEqual(call.kwargs["attempts"], 1)
        self.assertNotIn(SECRET, self.output.getvalue())

    def test_missing_authorization_fails_within_budget_before_reading_credentials(self):
        def invoke(*_args, **kwargs):
            self.assertLessEqual(kwargs["timeout"], 20)
            self.advance(min(5, kwargs["timeout"]))
            raise RuntimeError(SECRET)
        with patch("deploy_cluster.invoke", side_effect=invoke), \
             patch("deploy_cluster.resolve_inference") as resolve, \
             patch("deploy_cluster.vault_token") as vault, \
             patch.dict(os.environ, {"LAB_CONTEXT": json.dumps(CONTEXT)}), \
             patch("deploy_cluster.os.umask"):
            # Exercise the real entry-point ordering with a shorter auth budget.
            original_wait = deploy_cluster.wait_for_cluster_access
            with patch("deploy_cluster.wait_for_cluster_access", side_effect=lambda context: original_wait(context, timeout=20)):
                with self.assertRaisesRegex(RuntimeError, "setup identity|Setup identity") as error:
                    deploy_cluster.main()
            resolve.assert_not_called()
            vault.assert_not_called()
        self.assertEqual(self.clock, 20)
        self.assertNotIn(SECRET, str(error.exception) + self.output.getvalue())


class ClusterSetupScriptTests(unittest.TestCase):
    """Check bootstrap phase ordering and failure propagation with a command fixture."""
    def run_script(self, mode):
        with tempfile.TemporaryDirectory(prefix="ignite-cluster-script-") as folder:
            directory = Path(folder)
            kubectl = directory / "kubectl"
            kubectl.write_text("#!" + sys.executable + "\n" + r'''
import json, os, sys, time
from pathlib import Path
args = sys.argv[1:]
with Path("commands.jsonl").open("a") as log:
    log.write(json.dumps(args) + "\n")
mode = os.environ["IGNITE_TEST_LB_MODE"]
if "get" in args and "service" in args:
    # The old one-shot check sees no IP during initial provisioning.
    print("10.42.4.10" if mode == "ready" else "")
elif "wait" in args and "job/lab-readiness" in args and mode == "readiness-error":
    sys.exit(43)
elif "wait" in args and "service/aks-ops-mcp" in args:
    assert "-n" in args and args[args.index("-n") + 1] == "ignite-lab"
    assert "--timeout=300s" in args
    if mode in ("pending", "wrong-ip", "api-error"):
        sys.exit(42)
    if mode == "delayed":
        time.sleep(0.02)
    print("service/aks-ops-mcp condition met")
''')
            kubectl.chmod(0o700)
            script = directory / "cluster-setup.sh"
            script.write_text(deploy_cluster.cluster_setup_script())
            result = subprocess.run(["sh", str(script)], cwd=directory,
                env={**os.environ, "PATH": str(directory) + os.pathsep + os.environ["PATH"],
                     "IGNITE_TEST_LB_MODE": mode}, text=True, capture_output=True, timeout=10)
            commands = [json.loads(line) for line in (directory / "commands.jsonl").read_text().splitlines()]
            return result, commands

    def test_ready_and_delayed_ip_both_finish_after_exact_address_wait(self):
        for mode in ("ready", "delayed"):
            with self.subTest(mode=mode):
                result, commands = self.run_script(mode)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("real inference checks passed.", result.stdout)
                self.assertNotIn("IGNITE_SETUP_FAILURE", result.stdout)
                wait = next(i for i, args in enumerate(commands) if "service/aks-ops-mcp" in args)
                readiness = next(i for i, args in enumerate(commands) if "wait" in args and "job/lab-readiness" in args)
                self.assertGreater(wait, readiness)
                self.assertIn("wait", commands[wait])

    def test_pending_wrong_ip_and_api_errors_never_report_success(self):
        for mode in ("pending", "wrong-ip", "api-error"):
            with self.subTest(mode=mode):
                result, _ = self.run_script(mode)
                self.assertEqual(result.returncode, 42)
                self.assertIn("IGNITE_SETUP_FAILURE phase=load-balancer", result.stdout)
                self.assertNotIn("real inference checks passed.", result.stdout)

    def test_failed_readiness_stops_before_load_balancer_wait(self):
        result, commands = self.run_script("readiness-error")
        self.assertEqual(result.returncode, 43)
        self.assertIn("IGNITE_SETUP_FAILURE phase=readiness", result.stdout)
        self.assertFalse(any("service/aks-ops-mcp" in args for args in commands))
        self.assertNotIn("real inference checks passed.", result.stdout)


class CompiledSetupPermissionsTests(unittest.TestCase):
    """CI checks the ARM produced by Bicep, including scope and dependency order."""
    @classmethod
    def setUpClass(cls):
        filename = os.environ.get("IGNITE_COMPILED_TEMPLATE")
        if not filename:
            raise unittest.SkipTest("Compiled ARM checks run after the Bicep build in CI")
        cls.template = json.loads(Path(filename).read_text())
        resources = cls.template["resources"]
        cls.resources = list(resources.values()) if isinstance(resources, dict) else resources

    def test_setup_identity_has_data_plane_admin_only_at_this_cluster(self):
        roles = [resource for resource in self.resources
                 if resource["type"] == "Microsoft.Authorization/roleAssignments"
                 and any(name in resource.get("properties", {}).get("principalId", "")
                         for name in ("setupIdentity", "-setup-identity"))]
        admin = [resource for resource in roles
                 if "aksRbacClusterAdminRoleId" in resource["properties"]["roleDefinitionId"]]
        self.assertEqual(len(admin), 1)
        role = admin[0]
        self.assertEqual(role["condition"], "[parameters('deployLabSetup')]")
        self.assertIn("Microsoft.ContainerService/managedClusters", role["scope"])
        self.assertIn("aksName", role["scope"])
        self.assertEqual(role["properties"]["principalType"], "ServicePrincipal")
        self.assertIn("b1ff04bb-8a4e-4dc4-8eb5-8693973ce19b", self.template["variables"]["aksRbacClusterAdminRoleId"])
        script = next(resource for resource in self.resources if resource["type"] == "Microsoft.Resources/deploymentScripts")
        self.assertTrue(any("aksRbacClusterAdminRoleId" in value for value in script["dependsOn"]))

    def test_learner_identity_has_only_vault_access_and_script_uses_jq_runtime(self):
        learner_roles = [resource for resource in self.resources
                         if resource["type"] == "Microsoft.Authorization/roleAssignments"
                         and any(name in resource.get("properties", {}).get("principalId", "")
                                 for name in ("vmCredentialIdentity", "-credential-identity"))]
        self.assertEqual(len(learner_roles), 1)
        self.assertIn("Microsoft.KeyVault/vaults", learner_roles[0]["scope"])
        self.assertEqual(self.template["parameters"]["deploymentScriptAzCliVersion"]["defaultValue"], "2.65.0")


if __name__ == "__main__":
    unittest.main()
