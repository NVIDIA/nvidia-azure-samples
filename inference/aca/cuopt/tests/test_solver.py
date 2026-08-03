# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import httpx
import msgpack

from backend.solver import _response_body


def test_decodes_json_response() -> None:
    response = httpx.Response(200, json={"status": "completed"})

    assert _response_body(response) == {"status": "completed"}


def test_decodes_messagepack_response() -> None:
    body = {"status": "completed", "reqId": "job-1"}
    response = httpx.Response(
        200,
        content=msgpack.packb(body),
        headers={"Content-Type": "application/msgpack"},
    )

    assert _response_body(response) == body
