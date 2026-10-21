# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Connection-helper contract tests; no Azure login or network calls are made."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "infra/connect-cloud-shell.sh"
SUBSCRIPTION = "00000000-0000-0000-0000-000000000001"
GROUP = "lab-test"
NETWORK_ROOT = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{GROUP}/providers/Microsoft.Network"

# Azure CLI exposes NSG rule properties at the top level. This fake records
# all requests and persists the allow rule so SSH exit/cleanup can be tested.
FAKE_COMMAND = r'''
import json, os, pathlib, sys
state_path = pathlib.Path(os.environ['LAB_TEST_STATE'])
state = json.loads(state_path.read_text())
name, args = pathlib.Path(sys.argv[0]).name, sys.argv[1:]
with open(os.environ['LAB_TEST_CALLS'], 'a') as calls:
    calls.write(json.dumps([name, args]) + '\n')
def value(flag):
    return args[args.index(flag) + 1]
def save():
    state_path.write_text(json.dumps(state))
if name == 'curl':
    print(state.get('source_ip', '8.8.8.8'))
elif name == 'ssh':
    if state.get('replacement_source'):
        state['rule'] = {'sourceAddressPrefix': state['replacement_source']}
        save()
    sys.exit(state.get('ssh_exit', 0))
elif name == 'az':
    assert value('--subscription') == state['subscription']
    if args[:2] == ['vm', 'list']:
        assert value('--resource-group') == state['group']
        print(json.dumps(state['vms']))
    elif args[:3] == ['network', 'nic', 'show']:
        query = value('--query')
        result = {
            'ipConfigurations[0].subnet.id': state['subnet_id'],
            'ipConfigurations[0].publicIPAddress.id': state['public_ip_id'],
            'ipConfigurations[0].privateIPAddress': state['private_ip'],
        }[query]
        print(result)
    elif args[:4] == ['network', 'vnet', 'subnet', 'show']:
        print(state['nsg_id'])
    elif args[:3] == ['network', 'public-ip', 'show']:
        print(state['public_ip'])
    elif args[:4] == ['network', 'nsg', 'rule', 'create']:
        state['rule'] = {'sourceAddressPrefix': value('--source-address-prefixes')}
        save()
    elif args[:4] == ['network', 'nsg', 'rule', 'list']:
        print(json.dumps(state.get('rule')))
    elif args[:4] == ['network', 'nsg', 'rule', 'delete']:
        state['rule'] = None
        save()
    else:
        sys.exit('Unexpected fake Azure request: ' + repr(args))
else:
    sys.exit('Unexpected fake executable')
'''


class CloudShellConnectionTests(unittest.TestCase):
    def run_helper(self, *, changes=None, extra=()):
        with tempfile.TemporaryDirectory(prefix="ignite-ssh-test-") as folder:
            root = Path(folder)
            state = {
                "subscription": SUBSCRIPTION,
                "group": GROUP,
                "vms": [{"name": "ignite-vm", "username": "labuser",
                         "nicId": f"{NETWORK_ROOT}/networkInterfaces/lab-nic"}],
                "subnet_id": f"{NETWORK_ROOT}/virtualNetworks/lab-vnet/subnets/attendee",
                "nsg_id": f"{NETWORK_ROOT}/networkSecurityGroups/lab-vm-nsg",
                "public_ip_id": f"{NETWORK_ROOT}/publicIPAddresses/lab-vm-ip",
                # Public IPs are test data only; fake curl/SSH never contact them.
                "public_ip": "1.1.1.1",
                "private_ip": "10.42.1.4",
            }
            state.update(changes or {})
            state_file, calls_file = root / "state.json", root / "calls.jsonl"
            state_file.write_text(json.dumps(state))
            fake = root / "fake-command"
            fake.write_text(f"#!{sys.executable}\n{FAKE_COMMAND}")
            fake.chmod(0o755)
            for command in ("az", "curl", "ssh"):
                (root / command).symlink_to(fake)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}",
                       LAB_TEST_STATE=str(state_file), LAB_TEST_CALLS=str(calls_file))
            result = subprocess.run(
                ["bash", str(HELPER), "--subscription", SUBSCRIPTION,
                 "--resource-group", GROUP, *extra],
                env=env, capture_output=True, text=True, timeout=15)
            calls = [json.loads(line) for line in calls_file.read_text().splitlines()]
            return result, calls, json.loads(state_file.read_text())

    def mutations(self, calls, operation):
        return [args for command, args in calls if command == "az" and
                args[:4] == ["network", "nsg", "rule", operation]]

    def test_single_source_ssh_and_cleanup(self):
        result, calls, state = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        create, = self.mutations(calls, "create")
        expected = {"--source-address-prefixes": "8.8.8.8/32", "--protocol": "Tcp",
                    "--destination-port-ranges": "22", "--destination-address-prefixes": "10.42.1.4",
                    "--resource-group": GROUP, "--nsg-name": "lab-vm-nsg"}
        for flag, value in expected.items():
            self.assertEqual(create[create.index(flag) + 1], value)
        ssh, = [args for command, args in calls if command == "ssh"]
        self.assertEqual(ssh[-1], "labuser@1.1.1.1")
        self.assertFalse(any("StrictHostKeyChecking=no" in arg or "password" in arg.lower() for arg in ssh))
        self.assertEqual(len(self.mutations(calls, "delete")), 1)
        self.assertIsNone(state["rule"])

    def test_invalid_sources_never_open_ssh(self):
        for source in ("*", "0.0.0.0/0", "8.8.8.8/24", "10.1.2.3", "127.0.0.1", "::1", "224.0.0.1"):
            with self.subTest(source=source):
                result, calls, _ = self.run_helper(changes={"source_ip": source})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.mutations(calls, "create"), [])
                self.assertFalse(any(command == "ssh" for command, _ in calls))

    def test_rejects_network_resource_in_other_group(self):
        result, calls, _ = self.run_helper(changes={"nsg_id": NETWORK_ROOT.replace(
            f"resourceGroups/{GROUP}", "resourceGroups/another-lab") + "/networkSecurityGroups/other-nsg"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the assigned lab scope", result.stderr)
        self.assertEqual(self.mutations(calls, "create"), [])
        self.assertEqual(self.mutations(calls, "delete"), [])

    def test_rejects_ambiguous_vm_selection(self):
        vms = [{"name": name, "username": "labuser", "nicId": f"{NETWORK_ROOT}/networkInterfaces/lab-nic"}
               for name in ("first-vm", "second-vm")]
        result, calls, _ = self.run_helper(changes={"vms": vms})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.mutations(calls, "create"), [])
        result, _, _ = self.run_helper(changes={"vms": vms}, extra=("--vm", "second-vm"))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ssh_failure_still_closes_rule(self):
        result, calls, state = self.run_helper(changes={"ssh_exit": 255})
        self.assertEqual(result.returncode, 255)
        self.assertEqual(len(self.mutations(calls, "delete")), 1)
        self.assertIsNone(state["rule"])

    def test_preserves_replacement_rule(self):
        result, calls, state = self.run_helper(changes={"replacement_source": "9.9.9.9/32"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.mutations(calls, "delete"), [])
        self.assertEqual(state["rule"]["sourceAddressPrefix"], "9.9.9.9/32")

    def test_explicit_close_skips_source_lookup_and_ssh(self):
        result, calls, state = self.run_helper(changes={"rule": {"sourceAddressPrefix": "8.8.8.8/32"}},
                                             extra=("--close",))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.mutations(calls, "delete")), 1)
        self.assertIsNone(state["rule"])
        self.assertFalse(any(command in ("ssh", "curl") for command, _ in calls))


if __name__ == "__main__":
    unittest.main()
