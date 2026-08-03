# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Callable

import httpx
import msgpack

from .scenario import get_bundle, result_from_solver_response


class CuOptError(RuntimeError):
    pass


def _response_body(response: httpx.Response) -> Any:
    content_type = response.headers.get("content-type", "").lower()
    if "msgpack" in content_type or "octet-stream" in content_type:
        return msgpack.unpackb(response.content, raw=False)
    try:
        return response.json()
    except UnicodeDecodeError:
        return msgpack.unpackb(response.content, raw=False)


async def solve_disruption(
    time_limit_seconds: int,
    on_state: Callable[[str, str], None],
) -> tuple[dict[str, Any], float, float, str]:
    base_url = os.getenv("CUOPT_BASE_URL", "").rstrip("/")
    bundle = get_bundle("disrupted")
    if not base_url:
        on_state("waking_gpu", "Local replay mode: loading the recorded cuOpt recovery")
        await asyncio.sleep(0.05)
        on_state("validating", "Validating recorded route coverage and constraints")
        await asyncio.sleep(0.05)
        return bundle.recorded_result, 0.0, bundle.recorded_result["metrics"]["solve_seconds"], "recorded"

    wake_started = time.perf_counter()
    on_state("waking_gpu", "Waiting for the serverless A100 and cuOpt readiness")
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as client:
        deadline = time.monotonic() + int(os.getenv("CUOPT_WAKE_TIMEOUT_SECONDS", "720"))
        while True:
            try:
                response = await client.get(f"{base_url}/cuopt/health")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() >= deadline:
                raise CuOptError("cuOpt did not become ready before the cold-start timeout")
            await asyncio.sleep(2)
        wake_seconds = time.perf_counter() - wake_started

        payload = dict(bundle.payload)
        payload["solver_config"] = {"time_limit": time_limit_seconds}
        on_state("submitting", "Submitting the disrupted operating plan to cuOpt")
        response = await client.post(
            f"{base_url}/cuopt/request",
            json=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            timeout=60,
        )
        response.raise_for_status()
        body = _response_body(response)
        request_id = body.get("reqId")
        if not request_id:
            raise CuOptError("cuOpt did not return a request ID")

        on_state("solving", f"cuOpt job {request_id} is optimizing the recovery")
        solve_started = time.perf_counter()
        deadline = time.monotonic() + time_limit_seconds + 120
        while True:
            response = await client.get(
                f"{base_url}/cuopt/request/{request_id}",
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            status_body = _response_body(response)
            request_status = status_body.get("status") if isinstance(status_body, dict) else status_body
            if request_status == "completed":
                break
            if request_status in {"failed", "error"}:
                raise CuOptError(f"cuOpt job failed: {status_body}")
            if time.monotonic() >= deadline:
                raise CuOptError("cuOpt solution polling timed out")
            await asyncio.sleep(1)
        response = await client.get(
            f"{base_url}/cuopt/solution/{request_id}",
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        solution = _response_body(response)
        solve_seconds = time.perf_counter() - solve_started

    solver_response = solution.get("response", {}).get("solver_response", {})
    result = result_from_solver_response(solver_response, round(solve_seconds, 3))
    return result, wake_seconds, solve_seconds, "live"
