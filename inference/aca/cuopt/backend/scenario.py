# SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import copy
import math
import random
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

SEED = 20260803
ROUTE_COLORS = [
    "#76B900",
    "#00B4D8",
    "#F59E0B",
    "#A78BFA",
    "#FB7185",
    "#2DD4BF",
    "#60A5FA",
    "#F97316",
    "#C084FC",
    "#22C55E",
    "#EAB308",
    "#38BDF8",
    "#F472B6",
    "#84CC16",
]

DEPOTS = [
    {"id": "PHX", "name": "Phoenix Central", "latitude": 33.4484, "longitude": -112.0740},
    {"id": "TMP", "name": "Tempe East", "latitude": 33.4255, "longitude": -111.9400},
    {"id": "GLD", "name": "Glendale West", "latitude": 33.5387, "longitude": -112.1860},
]

CLUSTERS = [
    (33.4484, -112.0740, "Downtown Phoenix"),
    (33.4255, -111.9400, "Tempe"),
    (33.4942, -111.9261, "Scottsdale"),
    (33.4152, -111.8315, "Mesa"),
    (33.3062, -111.8413, "Chandler"),
    (33.5387, -112.1860, "Glendale"),
    (33.4353, -112.3577, "Goodyear"),
    (33.3528, -111.7890, "Gilbert"),
]


def _vehicle(vehicle_id: int, depot_index: int, kind: str, cold: bool = False) -> dict[str, Any]:
    if kind == "EV Van":
        weight, volume, max_km, cost_factor, time_factor = 900, 60, 210, 1.0, 1.0
    elif kind == "Refrigerated Truck":
        weight, volume, max_km, cost_factor, time_factor = 2200, 120, 300, 1.22, 1.12
    else:
        weight, volume, max_km, cost_factor, time_factor = 2500, 140, 320, 1.16, 1.08
    return {
        "id": f"FR-{vehicle_id:02d}",
        "vehicle_index": vehicle_id - 1,
        "type": kind,
        "type_index": 0 if kind == "EV Van" else 1,
        "depot_index": depot_index,
        "depot_id": DEPOTS[depot_index]["id"],
        "capacity_weight": weight,
        "capacity_volume": volume,
        "refrigerated": cold,
        "shift_start": 480,
        "shift_end": 1260,
        "break_start": 720,
        "break_end": 810,
        "break_duration": 45,
        "max_route_minutes": 780,
        "max_distance_km": max_km,
        "cost_factor": cost_factor,
        "time_factor": time_factor,
        "available": True,
        "color": ROUTE_COLORS[vehicle_id - 1],
    }


VEHICLES = [
    *[_vehicle(i + 1, i % 3, "EV Van") for i in range(8)],
    *[_vehicle(i + 9, (i + 1) % 3, "Box Truck") for i in range(4)],
    _vehicle(13, 0, "Refrigerated Truck", True),
    _vehicle(14, 1, "Refrigerated Truck", True),
]


def _haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371.0088 * 2 * math.asin(math.sqrt(h))


def _generate_orders() -> list[dict[str, Any]]:
    rng = random.Random(SEED)
    orders: list[dict[str, Any]] = []
    for idx in range(144):
        cluster_idx = idx % len(CLUSTERS)
        lat, lon, zone = CLUSTERS[cluster_idx]
        lat += rng.gauss(0, 0.018)
        lon += rng.gauss(0, 0.022)
        cold = idx % 7 == 0
        priority = idx >= 120
        start_band = [540, 600, 660, 720, 780][idx % 5]
        orders.append(
            {
                "id": f"ORD-{idx + 1:04d}",
                "order_index": idx,
                "name": f"Customer {idx + 1:04d}",
                "latitude": round(lat, 6),
                "longitude": round(lon, 6),
                "zone": zone,
                "demand_weight": rng.randint(24, 62),
                "demand_volume": rng.randint(2, 5),
                "service_minutes": rng.randint(5, 9),
                "window_start": start_band,
                # Wide, overlapping retail delivery windows keep the recorded
                # fixture feasible while still forcing cuOpt to sequence stops.
                "window_end": min(start_band + 720, 1260),
                "cold_chain": cold,
                "priority": priority,
                "active_morning": idx < 120,
            }
        )
    return orders


ORDERS = _generate_orders()


def _matrix(nodes: list[dict[str, Any]], type_factor: float, congestion: bool, travel: bool) -> list[list[float]]:
    result: list[list[float]] = []
    for left in nodes:
        row: list[float] = []
        for right in nodes:
            distance = _haversine_km(
                (left["latitude"], left["longitude"]),
                (right["latitude"], right["longitude"]),
            )
            if travel:
                value = distance / 34.0 * 60.0 * type_factor
                central = left.get("zone") == "Downtown Phoenix" or right.get("zone") == "Downtown Phoenix"
                cross_core = (left["longitude"] < -112.02) != (right["longitude"] < -112.02)
                if congestion and (central or cross_core):
                    value *= 1.55
            else:
                value = distance * type_factor
            row.append(round(value, 4))
        result.append(row)
    return result


def _nearest_depot(order: dict[str, Any]) -> int:
    return min(
        range(len(DEPOTS)),
        key=lambda index: _haversine_km(
            (order["latitude"], order["longitude"]),
            (DEPOTS[index]["latitude"], DEPOTS[index]["longitude"]),
        ),
    )


def _route_assignment(active_orders: list[dict[str, Any]], vehicles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    available = [vehicle for vehicle in vehicles if vehicle["available"]]
    routes: dict[str, list[dict[str, Any]]] = {vehicle["id"]: [] for vehicle in available}
    reefers = [vehicle for vehicle in available if vehicle["refrigerated"]]
    regulars = [vehicle for vehicle in available if not vehicle["refrigerated"]]

    for order in sorted(active_orders, key=lambda item: (not item["cold_chain"], item["zone"], item["id"])):
        candidates = reefers if order["cold_chain"] else regulars
        depot_index = _nearest_depot(order)
        local = [vehicle for vehicle in candidates if vehicle["depot_index"] == depot_index]
        pool = local or candidates
        vehicle = min(pool, key=lambda item: len(routes[item["id"]]))
        routes[vehicle["id"]].append(order)

    output: list[dict[str, Any]] = []
    for vehicle in available:
        stops = routes[vehicle["id"]]
        if not stops:
            continue
        depot = DEPOTS[vehicle["depot_index"]]
        stops.sort(key=lambda order: math.atan2(order["latitude"] - depot["latitude"], order["longitude"] - depot["longitude"]))
        output.append(_route_from_stops(vehicle, stops))
    return output


def _route_from_stops(vehicle: dict[str, Any], stops: list[dict[str, Any]], congestion: bool = False) -> dict[str, Any]:
    depot = DEPOTS[vehicle["depot_index"]]
    previous = depot
    minute = vehicle["shift_start"]
    distance = 0.0
    weight = sum(order["demand_weight"] for order in stops)
    volume = sum(order["demand_volume"] for order in stops)
    route_stops: list[dict[str, Any]] = []
    break_start_minute: int | None = None
    for order in stops:
        leg_km = _haversine_km(
            (previous["latitude"], previous["longitude"]),
            (order["latitude"], order["longitude"]),
        )
        travel_minutes = leg_km / 34.0 * 60.0 * vehicle["time_factor"]
        if congestion and (order["zone"] == "Downtown Phoenix" or previous.get("zone") == "Downtown Phoenix"):
            travel_minutes *= 1.55
        projected_arrival = int(round(minute + travel_minutes))
        if (
            break_start_minute is None
            and projected_arrival <= vehicle["break_end"]
            and max(projected_arrival, order["window_start"]) >= vehicle["break_start"]
        ):
            break_start_minute = max(projected_arrival, vehicle["break_start"])
            projected_arrival = break_start_minute + vehicle["break_duration"]
        minute = max(projected_arrival, order["window_start"])
        route_stops.append(
            {
                "order_id": order["id"],
                "node_index": order["order_index"] + 3,
                "latitude": order["latitude"],
                "longitude": order["longitude"],
                "arrival_minute": minute,
                "window_start": order["window_start"],
                "window_end": order["window_end"],
                "demand_weight": order["demand_weight"],
                "demand_volume": order["demand_volume"],
                "cold_chain": order["cold_chain"],
                "priority": order["priority"],
            }
        )
        minute += order["service_minutes"]
        distance += leg_km
        previous = order
    return_leg_km = _haversine_km(
        (previous["latitude"], previous["longitude"]),
        (depot["latitude"], depot["longitude"]),
    )
    distance += return_leg_km
    minute += return_leg_km / 34.0 * 60.0 * vehicle["time_factor"]
    return {
        "vehicle_id": vehicle["id"],
        "vehicle_type": vehicle["type"],
        "depot_id": vehicle["depot_id"],
        "color": vehicle["color"],
        "stops": route_stops,
        "total_distance_km": round(distance, 2),
        "total_travel_minutes": round(max(0, minute - vehicle["shift_start"]), 1),
        "weight_utilization": round(weight / vehicle["capacity_weight"], 3),
        "volume_utilization": round(volume / vehicle["capacity_volume"], 3),
        "break_start_minute": break_start_minute,
        "break_duration": vehicle["break_duration"] if break_start_minute is not None else 0,
    }


def validate_routes(
    routes: list[dict[str, Any]],
    active_orders: list[dict[str, Any]],
    vehicles: list[dict[str, Any]],
) -> dict[str, Any]:
    order_by_id = {order["id"]: order for order in active_orders}
    vehicle_by_id = {vehicle["id"]: vehicle for vehicle in vehicles}
    seen = [stop["order_id"] for route in routes for stop in route["stops"]]
    late = [
        stop["order_id"]
        for route in routes
        for stop in route["stops"]
        if stop["arrival_minute"] > order_by_id[stop["order_id"]]["window_end"]
    ]
    capacity_violations = [
        route["vehicle_id"]
        for route in routes
        if route["weight_utilization"] > 1 or route["volume_utilization"] > 1
    ]
    compatibility_violations = [
        stop["order_id"]
        for route in routes
        for stop in route["stops"]
        if order_by_id[stop["order_id"]]["cold_chain"]
        and not vehicle_by_id[route["vehicle_id"]]["refrigerated"]
    ]
    route_limit_violations = [
        route["vehicle_id"]
        for route in routes
        if route["total_travel_minutes"] > vehicle_by_id[route["vehicle_id"]]["max_route_minutes"]
    ]
    break_violations = [
        route["vehicle_id"]
        for route in routes
        if route["total_travel_minutes"] >= 240 and route.get("break_duration", 0) == 0
    ]
    return {
        "missing_orders": sorted(set(order_by_id) - set(seen)),
        "duplicate_orders": sorted({order_id for order_id in seen if seen.count(order_id) > 1}),
        "late_orders": late,
        "capacity_violations": capacity_violations,
        "compatibility_violations": compatibility_violations,
        "route_limit_violations": route_limit_violations,
        "break_violations": break_violations,
        "valid": len(seen) == len(order_by_id)
        and len(set(seen)) == len(seen)
        and not late
        and not capacity_violations
        and not compatibility_violations
        and not route_limit_violations
        and not break_violations,
    }


def _metrics(
    routes: list[dict[str, Any]],
    active_orders: list[dict[str, Any]],
    vehicles: list[dict[str, Any]],
    solve_seconds: float = 0.0,
) -> dict[str, Any]:
    stops = [stop for route in routes for stop in route["stops"]]
    validation = validate_routes(routes, active_orders, vehicles)
    vehicles_by_type: dict[str, int] = {}
    for route in routes:
        vehicles_by_type[route["vehicle_type"]] = vehicles_by_type.get(route["vehicle_type"], 0) + 1
    return {
        "orders": len(active_orders),
        "assigned_orders": len(stops),
        "dropped_orders": len(validation["missing_orders"]),
        "vehicles_used": len(routes),
        "vehicles_by_type": vehicles_by_type,
        "total_distance_km": round(sum(route["total_distance_km"] for route in routes), 1),
        "total_travel_minutes": round(sum(route["total_travel_minutes"] for route in routes), 1),
        "average_weight_utilization": round(sum(route["weight_utilization"] for route in routes) / len(routes), 3),
        "average_volume_utilization": round(sum(route["volume_utilization"] for route in routes) / len(routes), 3),
        "on_time_percentage": round(100 * (len(stops) - len(validation["late_orders"])) / max(1, len(stops)), 1),
        "solve_seconds": solve_seconds,
        "validated": validation["valid"],
    }


@dataclass(frozen=True)
class ScenarioBundle:
    state: str
    orders: list[dict[str, Any]]
    vehicles: list[dict[str, Any]]
    depots: list[dict[str, Any]]
    payload: dict[str, Any]
    recorded_result: dict[str, Any]


def _build_payload(active_orders: list[dict[str, Any]], vehicles: list[dict[str, Any]], congestion: bool) -> dict[str, Any]:
    nodes = [*DEPOTS, *active_orders]
    van_cost = _matrix(nodes, 1.0, False, False)
    truck_cost = _matrix(nodes, 1.16, False, False)
    van_time = _matrix(nodes, 1.0, congestion, True)
    truck_time = _matrix(nodes, 1.1, congestion, True)
    available = [vehicle for vehicle in vehicles if vehicle["available"]]
    cold_vehicle_ids = [index for index, vehicle in enumerate(available) if vehicle["refrigerated"]]
    cold_matches = [
        {"order_id": order["order_index"], "vehicle_ids": cold_vehicle_ids}
        for order in active_orders
        if order["cold_chain"]
    ]
    return {
        "cost_matrix_data": {"data": {"0": van_cost, "1": truck_cost}},
        "travel_time_matrix_data": {"data": {"0": van_time, "1": truck_time}},
        "task_data": {
            "task_locations": list(range(3, len(nodes))),
            "task_ids": [order["id"] for order in active_orders],
            "demand": [
                [order["demand_weight"] for order in active_orders],
                [order["demand_volume"] for order in active_orders],
            ],
            "task_time_windows": [[order["window_start"], order["window_end"]] for order in active_orders],
            "service_times": [order["service_minutes"] for order in active_orders],
            "order_vehicle_match": cold_matches,
        },
        "fleet_data": {
            "vehicle_locations": [[vehicle["depot_index"], vehicle["depot_index"]] for vehicle in available],
            "vehicle_ids": [vehicle["id"] for vehicle in available],
            "capacities": [
                [vehicle["capacity_weight"] for vehicle in available],
                [vehicle["capacity_volume"] for vehicle in available],
            ],
            "vehicle_time_windows": [[vehicle["shift_start"], vehicle["shift_end"]] for vehicle in available],
            "vehicle_break_time_windows": [
                [[vehicle["break_start"], vehicle["break_end"]] for vehicle in available]
            ],
            "vehicle_break_durations": [[vehicle["break_duration"] for vehicle in available]],
            "vehicle_max_times": [vehicle["max_route_minutes"] for vehicle in available],
            "vehicle_max_costs": [int(vehicle["max_distance_km"]) for vehicle in available],
            "vehicle_types": [vehicle["type_index"] for vehicle in available],
        },
        "solver_config": {"time_limit": 10},
    }


@lru_cache(maxsize=2)
def get_bundle(state: str) -> ScenarioBundle:
    if state not in {"morning", "disrupted"}:
        raise ValueError(f"Unknown scenario state: {state}")
    active_orders = copy.deepcopy(ORDERS[:120] if state == "morning" else ORDERS)
    vehicles = copy.deepcopy(VEHICLES)
    congestion = state == "disrupted"
    if congestion:
        vehicles[2]["available"] = False
    routes = _route_assignment(active_orders, vehicles)
    if congestion:
        by_id = {order["id"]: order for order in active_orders}
        vehicles_by_id = {vehicle["id"]: vehicle for vehicle in vehicles}
        routes = [
            _route_from_stops(vehicles_by_id[route["vehicle_id"]], [by_id[stop["order_id"]] for stop in route["stops"]], True)
            for route in routes
        ]
    result = {
        "scenario_state": state,
        "routes": routes,
        "metrics": _metrics(routes, active_orders, vehicles, 8.4 if congestion else 6.1),
        "recorded": True,
        "cuopt_image": "nvcr.io/nvidia/cuopt/cuopt:26.6.0-cuda12.9-py3.14",
    }
    return ScenarioBundle(
        state=state,
        orders=active_orders,
        vehicles=vehicles,
        depots=copy.deepcopy(DEPOTS),
        payload=_build_payload(active_orders, vehicles, congestion),
        recorded_result=result,
    )


def matrix_preview(state: str) -> dict[str, Any]:
    payload = get_bundle(state).payload
    return {
        "cost": {key: [row[:5] for row in value[:5]] for key, value in payload["cost_matrix_data"]["data"].items()},
        "travel_time": {
            key: [row[:5] for row in value[:5]] for key, value in payload["travel_time_matrix_data"]["data"].items()
        },
        "truncated": True,
    }


def disruption_impact() -> dict[str, Any]:
    morning = get_bundle("morning")
    unavailable_id = morning.vehicles[2]["id"]
    unavailable_orders = [
        stop["order_id"]
        for route in morning.recorded_result["routes"]
        if route["vehicle_id"] == unavailable_id
        for stop in route["stops"]
    ]
    return {
        "event_time": "2:15 PM",
        "new_priority_orders": 24,
        "unavailable_vehicle_id": unavailable_id,
        "orders_on_unavailable_route": unavailable_orders,
        "at_risk_orders": 24 + len(unavailable_orders),
        "predicted_late_orders": len(unavailable_orders),
        "cold_chain_violations": 0,
        "message": "The morning plan is no longer executable. Priority orders are unassigned and one route has no driver.",
    }


def result_from_solver_response(solver_response: dict[str, Any], solve_seconds: float) -> dict[str, Any]:
    if solver_response.get("status") != 0:
        raise ValueError(solver_response.get("msg") or "cuOpt did not return a feasible solution")

    bundle = get_bundle("disrupted")
    order_by_id = {order["id"]: order for order in bundle.orders}
    vehicle_by_id = {vehicle["id"]: vehicle for vehicle in bundle.vehicles}
    routes: list[dict[str, Any]] = []
    for vehicle_id, vehicle_solution in solver_response.get("vehicle_data", {}).items():
        if vehicle_id not in vehicle_by_id:
            continue
        ordered_ids = [
            str(task_id)
            for task_id, task_type in zip(
                vehicle_solution.get("task_id", []),
                vehicle_solution.get("type", []),
                strict=False,
            )
            if task_type == "Delivery" and str(task_id) in order_by_id
        ]
        if not ordered_ids:
            continue
        route = _route_from_stops(vehicle_by_id[vehicle_id], [order_by_id[order_id] for order_id in ordered_ids], True)
        arrival_by_id = {
            str(task_id): int(round(arrival))
            for task_id, task_type, arrival in zip(
                vehicle_solution.get("task_id", []),
                vehicle_solution.get("type", []),
                vehicle_solution.get("arrival_stamp", []),
                strict=False,
            )
            if task_type == "Delivery"
        }
        for stop in route["stops"]:
            stop["arrival_minute"] = arrival_by_id.get(stop["order_id"], stop["arrival_minute"])
        break_arrivals = [
            int(round(arrival))
            for task_type, arrival in zip(
                vehicle_solution.get("type", []),
                vehicle_solution.get("arrival_stamp", []),
                strict=False,
            )
            if task_type == "Break"
        ]
        route["break_start_minute"] = break_arrivals[0] if break_arrivals else None
        route["break_duration"] = vehicle_by_id[vehicle_id]["break_duration"] if break_arrivals else 0
        depot_arrivals = [
            float(arrival)
            for task_type, arrival in zip(
                vehicle_solution.get("type", []),
                vehicle_solution.get("arrival_stamp", []),
                strict=False,
            )
            if task_type == "Depot"
        ]
        if depot_arrivals:
            route["total_travel_minutes"] = round(depot_arrivals[-1] - vehicle_by_id[vehicle_id]["shift_start"], 1)
        routes.append(route)

    validation = validate_routes(routes, bundle.orders, bundle.vehicles)
    if not validation["valid"]:
        raise ValueError(f"cuOpt route validation failed: {validation}")
    return {
        "scenario_state": "disrupted",
        "routes": routes,
        "metrics": _metrics(routes, bundle.orders, bundle.vehicles, solve_seconds),
        "recorded": False,
        "cuopt_image": "nvcr.io/nvidia/cuopt/cuopt:26.6.0-cuda12.9-py3.14",
    }
