# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""Foundry discovery contracts; live Skillable/model validation is still required."""
import copy
import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'infra/setup'))
from deploy_cluster import foundry_api_base, resolve_inference


CONTEXT = {'resourceGroupName': 'instance-rg', 'foundryAccountName': 'myfoundry42',
           'resolveFoundryInference': True, 'upstreamInferenceEndpoint': '',
           'inferenceModel': 'NemotronLightning', 'inferenceAuthHeader': 'api-key'}
ACCOUNT = {'properties': {'provisioningState': 'Succeeded', 'endpoints': {
    'Azure OpenAI Legacy API - Latest moniker': 'https://myfoundry42.openai.azure.com/'}}}
DEPLOYMENT = {'properties': {'provisioningState': 'Succeeded'}}
KEY = 'test-only-foundry-secret'


class FoundryResolutionTests(unittest.TestCase):
    def test_discovery_uses_exact_account_model_and_keeps_key_out_of_context_and_output(self):
        output = io.StringIO()
        with patch('deploy_cluster.az', side_effect=[ACCOUNT, DEPLOYMENT, {'key1': KEY}]) as cli, redirect_stdout(output):
            context, key = resolve_inference(CONTEXT, '')
        self.assertEqual(key, KEY)
        self.assertEqual(context['upstreamInferenceEndpoint'], 'https://myfoundry42.openai.azure.com/openai/v1')
        self.assertEqual(context['inferenceAuthHeader'], 'api-key')
        self.assertNotIn(KEY, repr(context) + output.getvalue())
        self.assertEqual(CONTEXT['upstreamInferenceEndpoint'], '')
        for call in cli.call_args_list:
            self.assertIn('instance-rg', call.args)
            self.assertIn('myfoundry42', call.args)
            self.assertGreater(call.kwargs['timeout'], 0)
            self.assertLessEqual(call.kwargs['timeout'], 30)
        self.assertIn('NemotronLightning', cli.call_args_list[1].args)

    def test_selected_alternative_model_is_verified_without_switching_deployments(self):
        selected = {**CONTEXT, 'inferenceModel': 'Nemotron3Ultra'}
        with patch('deploy_cluster.az', side_effect=[ACCOUNT, DEPLOYMENT, {'key1': KEY}]) as cli, redirect_stdout(io.StringIO()):
            context, _ = resolve_inference(selected, '')
        self.assertEqual(context['inferenceModel'], 'Nemotron3Ultra')
        self.assertIn('Nemotron3Ultra', cli.call_args_list[1].args)

    def test_explicit_connection_is_preserved_and_does_not_read_foundry(self):
        explicit = {**CONTEXT, 'resolveFoundryInference': False,
                    'upstreamInferenceEndpoint': 'https://approved.example/v1', 'inferenceAuthHeader': 'Authorization'}
        with patch('deploy_cluster.az') as cli:
            context, key = resolve_inference(explicit, KEY)
        self.assertIs(context, explicit)
        self.assertEqual(key, KEY)
        cli.assert_not_called()

    def test_partial_or_mixed_connections_fail_before_any_key_lookup(self):
        variants = [({**CONTEXT, 'upstreamInferenceEndpoint': 'https://approved.example/v1'}, ''),
                    (CONTEXT, KEY), ({**CONTEXT, 'inferenceModel': ''}, ''),
                    ({**CONTEXT, 'foundryAccountName': ''}, ''),
                    ({**CONTEXT, 'resolveFoundryInference': False}, KEY)]
        for context, key in variants:
            with self.subTest(context=context, key_present=bool(key)), patch('deploy_cluster.az') as cli:
                with self.assertRaises(RuntimeError):
                    resolve_inference(context, key)
                cli.assert_not_called()

    def test_role_propagation_or_missing_account_retries_then_succeeds(self):
        clock = [0]
        def sleep(seconds):
            clock[0] += seconds
        with patch('deploy_cluster.time.monotonic', side_effect=lambda: clock[0]), \
             patch('deploy_cluster.time.sleep', side_effect=sleep), \
             patch('deploy_cluster.az', side_effect=[RuntimeError('temporarily unavailable'), ACCOUNT, DEPLOYMENT, {'key1': KEY}]) as cli, \
             redirect_stdout(io.StringIO()):
            context, key = resolve_inference(CONTEXT, '', timeout=20)
        self.assertEqual(clock[0], 10)
        self.assertEqual(cli.call_count, 4)
        self.assertEqual(key, KEY)
        self.assertIn('/openai/v1', context['upstreamInferenceEndpoint'])

    def test_pending_model_does_not_read_key_until_its_deployment_succeeds(self):
        clock = [0]
        with patch('deploy_cluster.time.monotonic', side_effect=lambda: clock[0]), \
             patch('deploy_cluster.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch('deploy_cluster.az', side_effect=[ACCOUNT, {'properties': {'provisioningState': 'Creating'}},
                                                    ACCOUNT, DEPLOYMENT, {'key1': KEY}]) as cli, redirect_stdout(io.StringIO()):
            _, key = resolve_inference(CONTEXT, '', timeout=20)
        self.assertEqual(key, KEY)
        self.assertEqual(cli.call_count, 5)
        self.assertEqual(cli.call_args_list[-1].args[:4], ('cognitiveservices', 'account', 'keys', 'list'))

    def test_repeated_failure_is_bounded_and_never_prints_remote_error_bodies(self):
        clock = [0]
        output = io.StringIO()
        with patch('deploy_cluster.time.monotonic', side_effect=lambda: clock[0]), \
             patch('deploy_cluster.time.sleep', side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds)), \
             patch('deploy_cluster.az', side_effect=RuntimeError('remote body contains ' + KEY)) as cli, redirect_stdout(output):
            with self.assertRaisesRegex(RuntimeError, 'discovery timed out') as error:
                resolve_inference(CONTEXT, '', timeout=20)
        self.assertEqual(clock[0], 20)
        self.assertEqual(cli.call_count, 2)
        self.assertNotIn(KEY, output.getvalue() + str(error.exception))

    def test_failed_model_or_disabled_key_auth_fails_before_reading_key(self):
        for account, deployment, message in [
            (ACCOUNT, {'properties': {'provisioningState': 'Failed'}}, 'provisioning failed'),
            ({'properties': {**ACCOUNT['properties'], 'disableLocalAuth': True}}, DEPLOYMENT, 'authentication is disabled')
        ]:
            with self.subTest(message=message), patch('deploy_cluster.az', side_effect=[account, deployment]) as cli, redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, message):
                    resolve_inference(CONTEXT, '')
                self.assertEqual(cli.call_count, 2)

    def test_only_inference_base_paths_are_accepted_and_normalized(self):
        for path in ('', '/', '/openai/', '/openai/v1/'):
            account = copy.deepcopy(ACCOUNT)
            account['properties']['endpoints']['Azure OpenAI Legacy API - Latest moniker'] = 'https://myfoundry42.openai.azure.com' + path
            self.assertEqual(foundry_api_base(account), 'https://myfoundry42.openai.azure.com/openai/v1')
        for endpoint in ('http://example.com', 'https://user:password@example.com',
                         'https://example.com/api/projects/example', 'https://example.com?api-key=secret',
                         'https://example.com/#fragment', ''):
            account = copy.deepcopy(ACCOUNT)
            account['properties']['endpoints']['Azure OpenAI Legacy API - Latest moniker'] = endpoint
            with self.subTest(endpoint=endpoint), self.assertRaises(RuntimeError):
                foundry_api_base(account)


if __name__ == '__main__':
    unittest.main()
