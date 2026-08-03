// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

export type Depot = {
  id: string
  name: string
  latitude: number
  longitude: number
}

export type Order = {
  id: string
  name: string
  latitude: number
  longitude: number
  zone: string
  demand_weight: number
  demand_volume: number
  service_minutes: number
  window_start: number
  window_end: number
  cold_chain: boolean
  priority: boolean
}

export type Vehicle = {
  id: string
  type: string
  depot_id: string
  capacity_weight: number
  capacity_volume: number
  refrigerated: boolean
  available: boolean
  color: string
}

export type RouteStop = {
  order_id: string
  node_index: number
  latitude: number
  longitude: number
  arrival_minute: number
  window_start: number
  window_end: number
  demand_weight: number
  demand_volume: number
  cold_chain: boolean
  priority: boolean
}

export type VehicleRoute = {
  vehicle_id: string
  vehicle_type: string
  depot_id: string
  color: string
  stops: RouteStop[]
  total_distance_km: number
  total_travel_minutes: number
  weight_utilization: number
  volume_utilization: number
}

export type Metrics = {
  orders: number
  assigned_orders: number
  dropped_orders: number
  vehicles_used: number
  vehicles_by_type: Record<string, number>
  total_distance_km: number
  total_travel_minutes: number
  average_weight_utilization: number
  average_volume_utilization: number
  on_time_percentage: number
  solve_seconds: number
  validated: boolean
}

export type SolverResult = {
  scenario_state: string
  routes: VehicleRoute[]
  metrics: Metrics
  recorded: boolean
  cuopt_image: string
}

export type Scenario = {
  id: string
  title: string
  subtitle: string
  story: string
  depots: Depot[]
  orders: Order[]
  vehicles: Vehicle[]
  constraints: string[]
  morning_result: SolverResult
}

export type Impact = {
  event_time: string
  new_priority_orders: number
  unavailable_vehicle_id: string
  orders_on_unavailable_route: string[]
  at_risk_orders: number
  predicted_late_orders: number
  cold_chain_violations: number
  message: string
}

export type Disruption = {
  orders: Order[]
  vehicles: Vehicle[]
  impact: Impact
  recorded_result: SolverResult
}

export type Job = {
  job_id: string
  state: 'queued' | 'waking_gpu' | 'submitting' | 'solving' | 'validating' | 'complete' | 'failed'
  mode: 'live' | 'recorded'
  message: string
  wake_seconds?: number
  solve_seconds?: number
  result?: SolverResult
  error?: string
}
