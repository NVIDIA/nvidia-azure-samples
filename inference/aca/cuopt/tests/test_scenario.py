# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from backend.scenario import disruption_impact, get_bundle, result_from_solver_response, validate_routes


def test_scenario_counts_and_fleet_mix() -> None:
    morning = get_bundle("morning")
    disrupted = get_bundle("disrupted")

    assert len(morning.orders) == 120
    assert len(disrupted.orders) == 144
    assert len(morning.vehicles) == 14
    assert sum(vehicle["type"] == "EV Van" for vehicle in morning.vehicles) == 8
    assert sum(vehicle["refrigerated"] for vehicle in morning.vehicles) == 2
    assert sum(vehicle["available"] for vehicle in disrupted.vehicles) == 13


def test_recorded_routes_are_independently_validated() -> None:
    for state in ("morning", "disrupted"):
        bundle = get_bundle(state)
        validation = validate_routes(bundle.recorded_result["routes"], bundle.orders, bundle.vehicles)
        assert validation == {
            "missing_orders": [],
            "duplicate_orders": [],
            "late_orders": [],
            "capacity_violations": [],
            "compatibility_violations": [],
            "route_limit_violations": [],
            "break_violations": [],
            "valid": True,
        }
        assert bundle.recorded_result["metrics"]["validated"] is True


def test_cuopt_payload_has_two_dimensions_and_two_vehicle_types() -> None:
    payload = get_bundle("disrupted").payload
    assert len(payload["task_data"]["task_locations"]) == 144
    assert len(payload["task_data"]["demand"]) == 2
    assert set(payload["cost_matrix_data"]["data"]) == {"0", "1"}
    assert set(payload["travel_time_matrix_data"]["data"]) == {"0", "1"}
    assert len(payload["fleet_data"]["vehicle_locations"]) == 13
    assert payload["task_data"]["order_vehicle_match"]


def test_disruption_makes_work_visible() -> None:
    impact = disruption_impact()
    assert impact["new_priority_orders"] == 24
    assert impact["unavailable_vehicle_id"] == "FR-03"
    assert impact["at_risk_orders"] > 24


def test_live_solver_response_adapter_uses_cuopt_route_order() -> None:
    bundle = get_bundle("disrupted")
    vehicle_data = {}
    for route in bundle.recorded_result["routes"]:
        vehicle_data[route["vehicle_id"]] = {
            "task_id": ["Depot", *[stop["order_id"] for stop in route["stops"]], "Break", "Depot"],
            "type": ["Depot", *["Delivery" for _ in route["stops"]], "Break", "Depot"],
            "arrival_stamp": [
                480,
                *[stop["arrival_minute"] for stop in route["stops"]],
                route["break_start_minute"],
                1200,
            ],
        }

    result = result_from_solver_response({"status": 0, "vehicle_data": vehicle_data}, 7.25)

    assert result["recorded"] is False
    assert result["metrics"]["assigned_orders"] == 144
    assert result["metrics"]["validated"] is True
    assert result["metrics"]["solve_seconds"] == 7.25
