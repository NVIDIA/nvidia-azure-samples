# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

import os

from fastapi.testclient import TestClient

from backend.app import app


client = TestClient(app)


def test_health_and_scenario() -> None:
    assert client.get("/api/health").status_code == 200
    response = client.get("/api/scenarios/freshroute-phoenix")
    assert response.status_code == 200
    assert response.json()["morning_result"]["metrics"]["assigned_orders"] == 120


def test_allowlist_rejects_unknown_principal() -> None:
    previous = os.environ.get("ALLOWED_PRINCIPAL_IDS")
    os.environ["ALLOWED_PRINCIPAL_IDS"] = "seller-object-id"
    try:
        assert client.get("/api/scenarios/freshroute-phoenix").status_code == 403
        assert client.get(
            "/api/scenarios/freshroute-phoenix",
            headers={"x-ms-client-principal-id": "seller-object-id"},
        ).status_code == 200
    finally:
        if previous is None:
            os.environ.pop("ALLOWED_PRINCIPAL_IDS", None)
        else:
            os.environ["ALLOWED_PRINCIPAL_IDS"] = previous


def test_recorded_job_completes() -> None:
    response = client.post("/api/jobs", json={"time_limit_seconds": 10})
    assert response.status_code == 202
    job = client.get(f"/api/jobs/{response.json()['job_id']}").json()
    assert job["state"] == "complete"
    assert job["mode"] == "recorded"
    assert job["result"]["metrics"]["assigned_orders"] == 144
