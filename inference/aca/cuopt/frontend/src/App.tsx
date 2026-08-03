// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import {
  ArrowRight,
  Braces,
  CheckCircle2,
  Clock3,
  CloudLightning,
  PackageCheck,
  Play,
  RefreshCw,
  Route,
  ShieldCheck,
  Snowflake,
  Truck,
  Users,
  Warehouse,
  XCircle,
  Zap,
} from 'lucide-react'
import { api } from './api'
import { FreshRouteMap } from './FreshRouteMap'
import { TechnicalDrawer } from './TechnicalDrawer'
import type { Disruption, Job, Scenario, SolverResult, VehicleRoute } from './types'

const jobSteps = ['waking_gpu', 'submitting', 'solving', 'validating', 'complete'] as const

export default function App() {
  const [scenario, setScenario] = useState<Scenario | null>(null)
  const [disruption, setDisruption] = useState<Disruption | null>(null)
  const [phase, setPhase] = useState<'morning' | 'broken' | 'recovered'>('morning')
  const [result, setResult] = useState<SolverResult | null>(null)
  const [job, setJob] = useState<Job | null>(null)
  const [selectedVehicle, setSelectedVehicle] = useState('')
  const [timeLimit, setTimeLimit] = useState<10 | 20 | 30>(10)
  const [drawerOpen, setDrawerOpen] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    Promise.all([api.scenario(), api.disruption()])
      .then(([base, event]) => {
        setScenario(base)
        setDisruption(event)
        setResult(base.morning_result)
      })
      .catch((cause) => setError(String(cause)))
  }, [])

  useEffect(() => {
    if (!job || ['complete', 'failed'].includes(job.state)) return
    const timer = window.setInterval(() => {
      api.job(job.job_id).then((next) => {
        setJob(next)
        if (next.state === 'complete' && next.result) {
          setResult(next.result)
          setPhase('recovered')
        }
        if (next.state === 'failed') setError(next.message)
      }).catch((cause) => setError(String(cause)))
    }, 700)
    return () => window.clearInterval(timer)
  }, [job])

  const selectVehicle = useCallback((vehicleId: string) => setSelectedVehicle(vehicleId), [])
  const activeOrders = phase === 'morning' ? scenario?.orders ?? [] : disruption?.orders ?? []
  const activeVehicles = phase === 'morning' ? scenario?.vehicles ?? [] : disruption?.vehicles ?? []
  const routes = result?.routes ?? []
  const selectedRoute = routes.find((route) => route.vehicle_id === selectedVehicle)

  const applyDisruption = async () => {
    setError('')
    await api.evaluate()
    setPhase('broken')
    setSelectedVehicle(disruption?.impact.unavailable_vehicle_id ?? '')
  }

  const recover = async () => {
    setError('')
    setSelectedVehicle('')
    try {
      const next = await api.createJob(timeLimit)
      setJob(next)
    } catch (cause) {
      setError(String(cause))
    }
  }

  const replay = () => {
    if (!disruption) return
    setResult(disruption.recorded_result)
    setPhase('recovered')
    setJob({
      job_id: 'recorded',
      state: 'complete',
      mode: 'recorded',
      message: 'Recorded cuOpt recovery loaded',
      solve_seconds: disruption.recorded_result.metrics.solve_seconds,
      result: disruption.recorded_result,
    })
  }

  const reset = async () => {
    await api.reset().catch(() => undefined)
    setPhase('morning')
    setResult(scenario?.morning_result ?? null)
    setJob(null)
    setSelectedVehicle('')
    setError('')
  }

  const mapRoutes = useMemo(() => {
    if (phase === 'broken') return scenario?.morning_result.routes ?? []
    return routes
  }, [phase, routes, scenario])

  if (!scenario || !disruption || !result) {
    return <main className="loading"><div className="loading-mark"><Route size={30} /><span /></div><p>{error || 'Preparing the FreshRoute operating plan…'}</p></main>
  }

  return (
    <main className={`app phase-${phase}`}>
      <header className="topbar">
        <div className="brand"><div className="brand-mark"><Route size={21} /></div><div><strong>FRESHROUTE</strong><span>PHOENIX CONTROL TOWER</span></div></div>
        <div className="topbar-center"><span className="live-dot" /> PRIVATE cuOPT NIM <i /> ACA A100 <i /> {phase === 'morning' ? 'MORNING PLAN' : phase === 'broken' ? 'DISRUPTION ACTIVE' : 'PLAN RECOVERED'}</div>
        <button className="technical-button" onClick={() => setDrawerOpen(true)}><Braces size={17} /> How this decision was made</button>
      </header>

      <section className="hero-strip">
        <div><span className="eyebrow">LIVE DELIVERY RECOVERY</span><h1>Protect the promise<br /><em>when the day changes.</em></h1></div>
        <p>{phase === 'morning' ? 'A feasible morning plan for 120 grocery and cold-chain deliveries across three metro depots.' : phase === 'broken' ? disruption.impact.message : 'cuOpt rebuilt the connected operating plan across every order, driver, depot, and constraint.'}</p>
      </section>

      <section className="workspace">
        <aside className="story-panel">
          <div className="time-card"><Clock3 size={18} /><div><span>OPERATING CLOCK</span><strong>{phase === 'morning' ? '11:40 AM' : '2:15 PM'}</strong></div><small>{phase === 'morning' ? 'Routes executing normally' : 'Promotion wave received'}</small></div>

          {phase === 'morning' && <MorningStory onApply={applyDisruption} metrics={result.metrics} />}
          {phase === 'broken' && <BrokenStory disruption={disruption} timeLimit={timeLimit} setTimeLimit={setTimeLimit} onRecover={recover} onReplay={replay} job={job} />}
          {phase === 'recovered' && <RecoveredStory result={result} job={job} onReset={reset} />}
          {error && <div className="error-card"><XCircle size={18} /><span>{error}</span></div>}
        </aside>

        <section className="map-shell">
          <FreshRouteMap
            depots={scenario.depots}
            orders={activeOrders}
            routes={mapRoutes}
            selectedVehicle={selectedVehicle}
            unavailableVehicle={phase === 'broken' ? disruption.impact.unavailable_vehicle_id : undefined}
            disrupted={phase !== 'morning'}
            onSelectVehicle={selectVehicle}
          />
          <div className="map-overlay map-status"><span className={`status-pulse ${phase}`} />{phase === 'morning' ? '120 orders · 14 vehicles · normal travel' : phase === 'broken' ? `+24 priority orders · ${disruption.impact.unavailable_vehicle_id} unavailable · congestion` : '144 orders · recovered network · validated'}</div>
          <div className="map-overlay legend"><span><i className="dot standard" /> Standard</span><span><i className="dot cold" /> Cold-chain</span>{phase !== 'morning' && <span><i className="dot priority" /> Priority</span>}<span><i className="line-swatch" /> Vehicle route</span></div>
        </section>

        <aside className="route-panel">
          <div className="panel-heading"><div><span className="eyebrow">ROUTE NETWORK</span><h2>{routes.length} active routes</h2></div><Truck size={22} /></div>
          <label className="vehicle-select-label">Inspect a vehicle<select value={selectedVehicle} onChange={(event) => setSelectedVehicle(event.target.value)}><option value="">All routes</option>{mapRoutes.map((route) => <option key={route.vehicle_id} value={route.vehicle_id}>{route.vehicle_id} · {route.vehicle_type}</option>)}</select></label>
          {selectedRoute ? <RouteDetail route={selectedRoute} /> : <FleetSummary routes={mapRoutes} />}
        </aside>
      </section>

      <TechnicalDrawer open={drawerOpen} state={phase === 'morning' ? 'morning' : 'disrupted'} orders={activeOrders} vehicles={activeVehicles} depots={scenario.depots} constraints={scenario.constraints} onClose={() => setDrawerOpen(false)} />
    </main>
  )
}

function MorningStory({ onApply, metrics }: { onApply: () => void; metrics: SolverResult['metrics'] }) {
  return <><div className="story-step"><span>01</span><div><h3>The morning promise</h3><p>Every customer has a time window. Every driver has two capacity limits, a shift, and a break.</p></div></div><div className="mini-metrics"><Metric icon={<PackageCheck />} value={String(metrics.assigned_orders)} label="assigned" /><Metric icon={<Truck />} value={String(metrics.vehicles_used)} label="routes" /><Metric icon={<ShieldCheck />} value={`${metrics.on_time_percentage.toFixed(0)}%`} label="on time" /></div><div className="constraint-stack"><span><Snowflake size={15} /> Cold-chain matching</span><span><Warehouse size={15} /> Three fulfillment depots</span><span><Users size={15} /> Driver shifts and breaks</span></div><button className="event-button" onClick={onApply}><CloudLightning size={20} /><span><small>2:15 PM EVENT</small>Apply the disruption</span><ArrowRight size={20} /></button></>
}

function BrokenStory({ disruption, timeLimit, setTimeLimit, onRecover, onReplay, job }: { disruption: Disruption; timeLimit: 10 | 20 | 30; setTimeLimit: (value: 10 | 20 | 30) => void; onRecover: () => void; onReplay: () => void; job: Job | null }) {
  const running = Boolean(job && !['complete', 'failed'].includes(job.state))
  return <><div className="alert-banner"><CloudLightning size={18} /><div><strong>THE PLAN IS BROKEN</strong><span>{disruption.impact.at_risk_orders} deliveries require a new decision</span></div></div><div className="impact-grid"><Impact value={`+${disruption.impact.new_priority_orders}`} label="priority orders" /><Impact value="1" label="driver callout" /><Impact value={String(disruption.impact.at_risk_orders)} label="at risk" /><Impact value="1.55×" label="core traffic" /></div><p className="story-copy">Fixing one route moves the problem somewhere else. Capacity, arrival times, breaks, depots, and cold-chain eligibility all move together.</p><label className="time-limit">cuOpt solve limit<div>{([10, 20, 30] as const).map((value) => <button key={value} className={timeLimit === value ? 'active' : ''} onClick={() => setTimeLimit(value)} disabled={running}>{value}s</button>)}</div></label>{running && <JobTimeline job={job!} />}<button className="recover-button" onClick={onRecover} disabled={running}><Zap size={21} /><span>{running ? 'cuOpt is rebuilding the plan…' : 'Recover the plan with cuOpt'}</span>{running ? <RefreshCw className="spin" size={19} /> : <Play size={19} />}</button><button className="replay-link" onClick={onReplay}>Presentation fallback: replay recorded recovery</button></>
}

function RecoveredStory({ result, job, onReset }: { result: SolverResult; job: Job | null; onReset: () => void }) {
  return <><div className="recovered-banner"><CheckCircle2 size={21} /><div><strong>CUSTOMER PROMISE RECOVERED</strong><span>{result.recorded ? 'Recorded cuOpt run' : 'Live cuOpt run'} · validated</span></div></div><div className="mini-metrics recovered"><Metric icon={<PackageCheck />} value={String(result.metrics.assigned_orders)} label="assigned" /><Metric icon={<Truck />} value={String(result.metrics.vehicles_used)} label="routes" /><Metric icon={<ShieldCheck />} value={`${result.metrics.on_time_percentage.toFixed(0)}%`} label="on time" /></div><div className="recovery-facts"><div><span>SOLVE TIME</span><strong>{(job?.solve_seconds ?? result.metrics.solve_seconds).toFixed(1)} sec</strong></div><div><span>DISTANCE</span><strong>{result.metrics.total_distance_km.toFixed(0)} km</strong></div><div><span>AVG. UTILIZATION</span><strong>{Math.round(result.metrics.average_weight_utilization * 100)}%</strong></div></div><p className="story-copy">The result is an executable operating plan—not a recommendation. Every order is assigned to a compatible vehicle with a feasible sequence.</p><button className="secondary-button" onClick={onReset}><RefreshCw size={18} /> Reset seller demo</button></>
}

function JobTimeline({ job }: { job: Job }) {
  const current = jobSteps.indexOf(job.state as (typeof jobSteps)[number])
  return <div className="job-timeline"><div>{jobSteps.map((step, index) => <span key={step} className={index <= current ? 'done' : ''}><i />{step.replace('_', ' ')}</span>)}</div><p>{job.message}</p></div>
}

function FleetSummary({ routes }: { routes: VehicleRoute[] }) {
  const types = routes.reduce<Record<string, number>>((result, route) => ({ ...result, [route.vehicle_type]: (result[route.vehicle_type] ?? 0) + 1 }), {})
  return <div className="fleet-summary"><div className="route-ribbon">{routes.map((route) => <i key={route.vehicle_id} style={{ background: route.color }} title={route.vehicle_id} />)}</div><p>Select a route on the map or choose a vehicle to inspect its sequence and operating constraints.</p>{Object.entries(types).map(([type, count]) => <div className="fleet-type" key={type}><span><i />{type}</span><strong>{count}</strong></div>)}</div>
}

function RouteDetail({ route }: { route: VehicleRoute }) {
  return <div className="route-detail"><div className="route-identity" style={{ borderColor: route.color }}><div><span>{route.vehicle_id}</span><strong>{route.vehicle_type}</strong></div><small>{route.depot_id} depot</small></div><div className="util-bars"><Util label="Weight" value={route.weight_utilization} color={route.color} /><Util label="Volume" value={route.volume_utilization} color="#38bdf8" /></div><div className="route-stats"><span>{route.stops.length}<small>stops</small></span><span>{route.total_distance_km.toFixed(1)}<small>km</small></span><span>{Math.round(route.total_travel_minutes)}<small>min</small></span></div><div className="stop-list">{route.stops.map((stop, index) => <div key={stop.order_id}><i style={{ borderColor: route.color }}>{index + 1}</i><span><strong>{stop.order_id}</strong><small>{formatTime(stop.arrival_minute)} · {stop.demand_weight} kg {stop.cold_chain ? '· cold' : ''}</small></span>{stop.priority && <em>PRIORITY</em>}</div>)}</div></div>
}

function Metric({ icon, value, label }: { icon: ReactNode; value: string; label: string }) { return <div>{icon}<strong>{value}</strong><span>{label}</span></div> }
function Impact({ value, label }: { value: string; label: string }) { return <div><strong>{value}</strong><span>{label}</span></div> }
function Util({ label, value, color }: { label: string; value: number; color: string }) { return <div><span>{label}<strong>{Math.round(value * 100)}%</strong></span><i><b style={{ width: `${Math.min(value * 100, 100)}%`, background: color }} /></i></div> }
function formatTime(minutes: number) { const hour = Math.floor(minutes / 60); return `${hour % 12 || 12}:${(minutes % 60).toString().padStart(2, '0')} ${hour >= 12 ? 'PM' : 'AM'}` }
