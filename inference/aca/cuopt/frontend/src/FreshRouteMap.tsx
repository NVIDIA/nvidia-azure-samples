// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from 'react'
import * as atlas from 'azure-maps-control'
import 'azure-maps-control/dist/atlas.min.css'
import { api } from './api'
import type { Depot, Order, VehicleRoute } from './types'

type Props = {
  depots: Depot[]
  orders: Order[]
  routes: VehicleRoute[]
  selectedVehicle: string
  unavailableVehicle?: string
  disrupted: boolean
  onSelectVehicle: (vehicleId: string) => void
}

export function FreshRouteMap({
  depots,
  orders,
  routes,
  selectedVehicle,
  unavailableVehicle,
  disrupted,
  onSelectVehicle,
}: Props) {
  const elementRef = useRef<HTMLDivElement>(null)
  const mapRef = useRef<atlas.Map | null>(null)
  const sourceRef = useRef<atlas.source.DataSource | null>(null)
  const [fallback, setFallback] = useState(false)
  const [mapReady, setMapReady] = useState(false)

  useEffect(() => {
    if (!elementRef.current || mapRef.current) return
    let disposed = false
    api
      .mapsToken()
      .then((auth) => {
        if (disposed || !elementRef.current) return
        const map = new atlas.Map(elementRef.current, {
          center: [-112.074, 33.4484],
          zoom: 9.4,
          style: 'grayscale_dark',
          language: 'en-US',
          authOptions: {
            authType: atlas.AuthenticationType.anonymous,
            clientId: auth.clientId,
            getToken: (resolve) => {
              api.mapsToken().then(({ token }) => resolve(token))
            },
          },
        })
        mapRef.current = map
        map.events.add('ready', () => {
          const source = new atlas.source.DataSource('freshroute')
          sourceRef.current = source
          map.sources.add(source)

          const routeLayer = new atlas.layer.LineLayer(source, 'routes', {
            filter: ['==', ['geometry-type'], 'LineString'],
            strokeColor: ['get', 'color'],
            strokeOpacity: ['get', 'opacity'],
            strokeWidth: ['get', 'width'],
          })
          map.layers.add(routeLayer)
          map.layers.add(
            new atlas.layer.BubbleLayer(source, 'orders', {
              filter: ['all', ['==', ['geometry-type'], 'Point'], ['==', ['get', 'kind'], 'order']],
              color: ['case', ['get', 'priority'], '#fb923c', ['get', 'cold'], '#38bdf8', '#d7e4d2'],
              radius: ['case', ['get', 'priority'], 5, 3],
              strokeColor: '#07100b',
              strokeWidth: 1,
              opacity: 0.86,
            }),
          )
          map.layers.add(
            new atlas.layer.SymbolLayer(source, 'depots', {
              filter: ['==', ['get', 'kind'], 'depot'],
              iconOptions: { image: 'marker-green', allowOverlap: true },
              textOptions: { textField: ['get', 'label'], offset: [0, 1.2], color: '#ffffff', size: 12 },
            }),
          )
          map.events.add('click', routeLayer, (event: atlas.MapMouseEvent) => {
            const shape = event.shapes?.[0]
            const properties = shape instanceof atlas.Shape ? shape.getProperties() : undefined
            if (properties?.vehicleId) onSelectVehicle(properties.vehicleId as string)
          })
          setMapReady(true)
        })
      })
      .catch(() => setFallback(true))
    return () => {
      disposed = true
      mapRef.current?.dispose()
      mapRef.current = null
      sourceRef.current = null
    }
  }, [onSelectVehicle])

  useEffect(() => {
    const source = sourceRef.current
    if (!source) return
    source.clear()
    const shapes: atlas.data.Feature<atlas.data.Geometry, unknown>[] = []
    for (const depot of depots) {
      shapes.push(
        new atlas.data.Feature(new atlas.data.Point([depot.longitude, depot.latitude]), {
          kind: 'depot',
          label: depot.id,
        }),
      )
    }
    for (const order of orders) {
      shapes.push(
        new atlas.data.Feature(new atlas.data.Point([order.longitude, order.latitude]), {
          kind: 'order',
          priority: order.priority && disrupted,
          cold: order.cold_chain,
          orderId: order.id,
        }),
      )
    }
    for (const route of routes) {
      const depot = depots.find((item) => item.id === route.depot_id)
      if (!depot || route.stops.length === 0) continue
      const selected = selectedVehicle === route.vehicle_id
      const muted = Boolean(selectedVehicle && !selected)
      const unavailable = route.vehicle_id === unavailableVehicle && disrupted
      const coordinates: atlas.data.Position[] = [
        [depot.longitude, depot.latitude],
        ...route.stops.map((stop) => [stop.longitude, stop.latitude] as atlas.data.Position),
        [depot.longitude, depot.latitude],
      ]
      shapes.push(
        new atlas.data.Feature(new atlas.data.LineString(coordinates), {
          vehicleId: route.vehicle_id,
          color: unavailable ? '#ef4444' : route.color,
          opacity: muted ? 0.08 : selected ? 0.98 : 0.46,
          width: selected ? 6 : unavailable ? 5 : 2.5,
        }),
      )
    }
    source.add(shapes)
  }, [depots, disrupted, mapReady, orders, routes, selectedVehicle, unavailableVehicle])

  if (fallback) {
    return (
      <div className="map-fallback" role="img" aria-label="Azure Maps needs deployment credentials">
        <div className="fallback-grid" />
        <div>
          <span>AZURE MAPS</span>
          <strong>Map credentials are not configured locally</strong>
          <p>The live deployment uses short-lived Entra tokens. Route data and controls remain available.</p>
        </div>
      </div>
    )
  }

  return <div ref={elementRef} className="map" aria-label="FreshRoute Phoenix delivery routes" />
}
