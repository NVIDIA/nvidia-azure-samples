# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import JobRecord, JobState, SolveRequest
from .scenario import disruption_impact, get_bundle, matrix_preview
from .solver import solve_disruption

APP_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_DIST = APP_ROOT / "frontend" / "dist"

app = FastAPI(title="FreshRoute cuOpt Demo", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin for origin in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",") if origin],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

jobs: dict[str, JobRecord] = {}
job_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_user(x_ms_client_principal_id: str | None = Header(default=None)) -> str:
    allowed = {value.strip() for value in os.getenv("ALLOWED_PRINCIPAL_IDS", "").split(",") if value.strip()}
    if not allowed:
        return x_ms_client_principal_id or "local-developer"
    if not x_ms_client_principal_id or x_ms_client_principal_id not in allowed:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account is not allowed to run the demo")
    return x_ms_client_principal_id


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "freshroute"}


@app.get("/api/scenarios/freshroute-phoenix")
def scenario(_: str = Depends(require_user)) -> dict[str, Any]:
    morning = get_bundle("morning")
    return {
        "id": "freshroute-phoenix",
        "title": "FreshRoute Phoenix",
        "subtitle": "Same-day grocery and cold-chain recovery",
        "story": "At 2:15 PM, 24 priority orders arrive, an EV driver calls out, and central Phoenix becomes congested.",
        "depots": morning.depots,
        "orders": morning.orders,
        "vehicles": morning.vehicles,
        "morning_result": morning.recorded_result,
        "constraints": [
            "Weight capacity",
            "Volume capacity",
            "Customer time windows",
            "Driver shifts and breaks",
            "Maximum route time",
            "Cold-chain vehicle matching",
            "Multiple depots",
        ],
    }


@app.get("/api/scenarios/freshroute-phoenix/disruption")
def disruption(_: str = Depends(require_user)) -> dict[str, Any]:
    bundle = get_bundle("disrupted")
    return {
        "orders": bundle.orders,
        "vehicles": bundle.vehicles,
        "impact": disruption_impact(),
        "recorded_result": bundle.recorded_result,
    }


@app.get("/api/scenarios/freshroute-phoenix/payload")
def payload(state: str = "disrupted", _: str = Depends(require_user)) -> Response:
    if state not in {"morning", "disrupted"}:
        raise HTTPException(status_code=400, detail="state must be morning or disrupted")
    return Response(json.dumps(get_bundle(state).payload, indent=2), media_type="application/json")


@app.get("/api/scenarios/freshroute-phoenix/matrix-preview")
def matrices(state: str = "disrupted", _: str = Depends(require_user)) -> dict[str, Any]:
    if state not in {"morning", "disrupted"}:
        raise HTTPException(status_code=400, detail="state must be morning or disrupted")
    return matrix_preview(state)


@app.post("/api/disruption/evaluate")
def evaluate(_: str = Depends(require_user)) -> dict[str, Any]:
    return disruption_impact()


def _set_job_state(job_id: str, state_value: str, message: str) -> None:
    job = jobs[job_id]
    job.state = JobState(state_value)
    job.message = message
    job.updated_at = _now()


async def _run_job(job_id: str, time_limit_seconds: int) -> None:
    try:
        result, wake_seconds, solve_seconds, mode = await solve_disruption(
            time_limit_seconds,
            lambda state_value, message: _set_job_state(job_id, state_value, message),
        )
        _set_job_state(job_id, "validating", "Checking every order, route, capacity, time window, and vehicle match")
        await asyncio.sleep(0)
        job = jobs[job_id]
        job.mode = mode
        job.wake_seconds = round(wake_seconds, 3)
        job.solve_seconds = round(solve_seconds, 3)
        job.result = result
        job.state = JobState.COMPLETE
        job.message = "Recovery plan validated and ready"
        job.updated_at = _now()
    except Exception as exc:  # noqa: BLE001 - convert solver failures into a stable job response
        job = jobs[job_id]
        job.state = JobState.FAILED
        job.error = str(exc)
        job.message = "The live recovery failed. Use the recorded fallback to continue the presentation."
        job.updated_at = _now()
    finally:
        if job_lock.locked():
            job_lock.release()


@app.post("/api/jobs", response_model=JobRecord, status_code=202)
async def create_job(request: SolveRequest, background_tasks: BackgroundTasks, _: str = Depends(require_user)) -> JobRecord:
    if job_lock.locked():
        raise HTTPException(status_code=429, detail="A recovery is already running", headers={"Retry-After": "5"})
    await job_lock.acquire()
    timestamp = _now()
    job = JobRecord(
        job_id=str(uuid.uuid4()),
        state=JobState.QUEUED,
        created_at=timestamp,
        updated_at=timestamp,
        message="Recovery queued",
    )
    jobs[job.job_id] = job
    background_tasks.add_task(_run_job, job.job_id, request.time_limit_seconds)
    return job


@app.get("/api/jobs/{job_id}", response_model=JobRecord)
def get_job(job_id: str, _: str = Depends(require_user)) -> JobRecord:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.post("/api/demo/reset")
def reset_demo(_: str = Depends(require_user)) -> dict[str, str]:
    if job_lock.locked():
        raise HTTPException(status_code=409, detail="Cannot reset while a recovery is running")
    jobs.clear()
    return {"status": "reset"}


@app.get("/api/maps/token")
def maps_token(_: str = Depends(require_user)) -> dict[str, Any]:
    client_id = os.getenv("AZURE_MAPS_CLIENT_ID", "")
    static_token = os.getenv("AZURE_MAPS_TOKEN", "")
    if static_token:
        return {"clientId": client_id, "token": static_token, "expiresIn": 300}
    try:
        from azure.identity import DefaultAzureCredential

        token = DefaultAzureCredential().get_token("https://atlas.microsoft.com/.default")
        return {"clientId": client_id, "token": token.token, "expiresOn": token.expires_on}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"Azure Maps authentication is not configured: {exc}") from exc


if FRONTEND_DIST.exists():
    assets = FRONTEND_DIST / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        requested = FRONTEND_DIST / path
        if path and requested.is_file():
            return FileResponse(requested)
        return FileResponse(FRONTEND_DIST / "index.html")
