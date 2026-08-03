// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useState } from 'react'
import { Braces, Database, Truck, X } from 'lucide-react'
import { api } from './api'
import type { Depot, Order, Vehicle } from './types'

type Props = {
  open: boolean
  state: 'morning' | 'disrupted'
  orders: Order[]
  vehicles: Vehicle[]
  depots: Depot[]
  constraints: string[]
  onClose: () => void
}

type Tab = 'orders' | 'fleet' | 'matrices' | 'payload'

export function TechnicalDrawer({ open, state, orders, vehicles, depots, constraints, onClose }: Props) {
  const [tab, setTab] = useState<Tab>('orders')
  const [payload, setPayload] = useState('')
  const [matrices, setMatrices] = useState<Record<string, unknown>>({})

  useEffect(() => {
    if (!open) return
    if (tab === 'payload') api.payloadText(state).then(setPayload).catch((error) => setPayload(String(error)))
    if (tab === 'matrices') api.matrixPreview(state).then(setMatrices).catch((error) => setMatrices({ error: String(error) }))
  }, [open, state, tab])

  if (!open) return null
  return (
    <div className="drawer-backdrop" onMouseDown={onClose}>
      <aside className="technical-drawer" onMouseDown={(event) => event.stopPropagation()} aria-label="How this decision was made">
        <header>
          <div>
            <span className="eyebrow">DECISION TRANSPARENCY</span>
            <h2>How this plan is built</h2>
          </div>
          <button className="icon-button" onClick={onClose} aria-label="Close technical details"><X size={20} /></button>
        </header>
        <div className="constraint-row">{constraints.map((item) => <span key={item}>{item}</span>)}</div>
        <nav className="drawer-tabs" aria-label="Technical detail sections">
          <button className={tab === 'orders' ? 'active' : ''} onClick={() => setTab('orders')}><Database size={16} /> Orders</button>
          <button className={tab === 'fleet' ? 'active' : ''} onClick={() => setTab('fleet')}><Truck size={16} /> Fleet & depots</button>
          <button className={tab === 'matrices' ? 'active' : ''} onClick={() => setTab('matrices')}><Database size={16} /> Matrices</button>
          <button className={tab === 'payload' ? 'active' : ''} onClick={() => setTab('payload')}><Braces size={16} /> cuOpt JSON</button>
        </nav>
        <div className="drawer-content">
          {tab === 'orders' && (
            <div className="table-wrap"><table><thead><tr><th>Order</th><th>Zone</th><th>Window</th><th>Weight</th><th>Volume</th><th>Cold</th></tr></thead><tbody>
              {orders.slice(0, 50).map((order) => <tr key={order.id}><td>{order.id}</td><td>{order.zone}</td><td>{formatTime(order.window_start)}–{formatTime(order.window_end)}</td><td>{order.demand_weight} kg</td><td>{order.demand_volume}</td><td>{order.cold_chain ? 'Required' : '—'}</td></tr>)}
            </tbody></table><p className="table-note">Showing 50 of {orders.length} orders.</p></div>
          )}
          {tab === 'fleet' && (
            <><div className="table-wrap"><table><thead><tr><th>Vehicle</th><th>Type</th><th>Depot</th><th>Weight</th><th>Volume</th><th>Status</th></tr></thead><tbody>
              {vehicles.map((vehicle) => <tr key={vehicle.id}><td>{vehicle.id}</td><td>{vehicle.type}</td><td>{vehicle.depot_id}</td><td>{vehicle.capacity_weight} kg</td><td>{vehicle.capacity_volume}</td><td>{vehicle.available ? 'Available' : 'Called out'}</td></tr>)}
            </tbody></table></div><div className="depot-list">{depots.map((depot) => <div key={depot.id}><strong>{depot.id}</strong><span>{depot.name}</span></div>)}</div></>
          )}
          {tab === 'matrices' && <pre>{JSON.stringify(matrices, null, 2)}</pre>}
          {tab === 'payload' && <pre>{payload}</pre>}
        </div>
      </aside>
    </div>
  )
}

function formatTime(minutes: number) {
  const hour = Math.floor(minutes / 60)
  const minute = minutes % 60
  return `${hour % 12 || 12}:${minute.toString().padStart(2, '0')} ${hour >= 12 ? 'PM' : 'AM'}`
}
