# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

"""A successful HTTP response must include a usable, finished inference reply."""
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "infra/setup"))
from check_cluster import check


class ClusterReadinessTests(unittest.TestCase):
    def run_check(self, inference_response, *, host=None):
        model_requests = []
        context = {"mcpPrivateIp": "10.42.4.10", "inferenceModel": "assigned-model"}
        tools = {
            "get_desired_config": {"data": {"DEPENDENCY_URL": "http://dependency-api:8081"}},
            "get_runtime_config": {"effective_dependency_url": "http://dependency-api-invalid:8081"},
            "verify_application": {"status": 503},
            "rbac_self_test": {"isolation_enforced": True},
        }

        def open_response(request, **_kwargs):
            if isinstance(request, str):
                self.assertEqual(request, "http://dependency-api:8081/healthz")
                response = io.BytesIO(b"{}")
            else:
                body = json.loads(request.data)
                if request.full_url.endswith("/mcp"):
                    if request.get_header("Authorization") == "Bearer invalid-probe-token":
                        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, io.BytesIO())
                    payload = {"result": {"content": [{"text": json.dumps(tools[body["params"]["name"]])}]}}
                else:
                    self.assertTrue(request.full_url.endswith(":8000/v1/chat/completions"))
                    self.assertEqual(request.get_header("Authorization"), "Bearer test-broker-token")
                    model_requests.append(body)
                    payload = inference_response
                response = io.BytesIO(json.dumps(payload).encode())
            response.status = 200
            return response

        with patch("check_cluster.ssl.create_default_context", return_value=object()), \
                patch("check_cluster.urllib.request.urlopen", side_effect=open_response):
            result = check(context, Path("test-ca.crt"), "test-mcp-token", "test-broker-token", host)
        return result, model_requests

    def test_finished_visible_reply_passes_for_vm_and_incluster_probes(self):
        response = {"choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}]}
        for host in (None, "aks-ops-mcp"):
            with self.subTest(host=host):
                result, requests = self.run_check(response, host=host)
                self.assertEqual(result["inference"], "ready")
                self.assertEqual(requests[0]["model"], "assigned-model")
                self.assertEqual(requests[0]["max_tokens"], 1024)
                self.assertFalse(requests[0]["stream"])

    def test_observed_reasoning_only_response_does_not_claim_readiness(self):
        response = {"choices": [{"message": {"content": "", "reasoning_content": "Thinking about the request"},
                                 "finish_reason": "length"}],
                    "usage": {"completion_tokens": 128,
                              "completion_tokens_details": {"reasoning_tokens": 127}}}
        with self.assertRaisesRegex(AssertionError, "visible reply"):
            self.run_check(response)

    def test_truncated_visible_reply_does_not_claim_readiness(self):
        with self.assertRaisesRegex(AssertionError, "finish its reply"):
            self.run_check({"choices": [{"message": {"content": "rea"}, "finish_reason": "length"}]})

    def test_missing_choices_do_not_claim_readiness(self):
        for response in ({}, {"choices": []}, {"choices": None}):
            with self.subTest(response=response), self.assertRaisesRegex(AssertionError, "completion"):
                self.run_check(response)

    def test_missing_or_blank_content_does_not_claim_readiness(self):
        for message in ({}, {"content": None}, {"content": " \n\t"}):
            with self.subTest(message=message), self.assertRaisesRegex(AssertionError, "visible reply"):
                self.run_check({"choices": [{"message": message, "finish_reason": "stop"}]})


if __name__ == "__main__":
    unittest.main()
