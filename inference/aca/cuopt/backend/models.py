# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class JobState(str, Enum):
    QUEUED = "queued"
    WAKING_GPU = "waking_gpu"
    SUBMITTING = "submitting"
    SOLVING = "solving"
    VALIDATING = "validating"
    COMPLETE = "complete"
    FAILED = "failed"


class SolveRequest(BaseModel):
    scenario_state: Literal["disrupted"] = "disrupted"
    time_limit_seconds: Literal[10, 20, 30] = 10


class JobRecord(BaseModel):
    job_id: str
    state: JobState
    created_at: str
    updated_at: str
    mode: Literal["live", "recorded"] = "live"
    message: str = ""
    wake_seconds: float | None = None
    solve_seconds: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class RouteStop(BaseModel):
    order_id: str
    node_index: int
    latitude: float
    longitude: float
    arrival_minute: int
    demand_weight: int
    demand_volume: int
    cold_chain: bool


class VehicleRoute(BaseModel):
    vehicle_id: str
    vehicle_type: str
    depot_id: str
    color: str
    stops: list[RouteStop]
    total_distance_km: float = Field(ge=0)
    total_travel_minutes: float = Field(ge=0)
    weight_utilization: float = Field(ge=0)
    volume_utilization: float = Field(ge=0)
