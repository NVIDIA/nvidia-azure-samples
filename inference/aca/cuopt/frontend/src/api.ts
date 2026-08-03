// SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

import type { Disruption, Impact, Job, Scenario } from './types'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', ...(options?.headers ?? {}) },
    ...options,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(body.detail ?? `Request failed (${response.status})`)
  }
  return response.json() as Promise<T>
}

export const api = {
  scenario: () => request<Scenario>('/api/scenarios/freshroute-phoenix'),
  disruption: () => request<Disruption>('/api/scenarios/freshroute-phoenix/disruption'),
  evaluate: () => request<Impact>('/api/disruption/evaluate', { method: 'POST' }),
  createJob: (timeLimitSeconds: 10 | 20 | 30) =>
    request<Job>('/api/jobs', {
      method: 'POST',
      body: JSON.stringify({ scenario_state: 'disrupted', time_limit_seconds: timeLimitSeconds }),
    }),
  job: (jobId: string) => request<Job>(`/api/jobs/${jobId}`),
  reset: () => request<{ status: string }>('/api/demo/reset', { method: 'POST' }),
  mapsToken: () => request<{ clientId: string; token: string; expiresOn?: number }>('/api/maps/token'),
  matrixPreview: (state: 'morning' | 'disrupted') =>
    request<Record<string, unknown>>(`/api/scenarios/freshroute-phoenix/matrix-preview?state=${state}`),
  payloadText: async (state: 'morning' | 'disrupted') => {
    const response = await fetch(`/api/scenarios/freshroute-phoenix/payload?state=${state}`, { credentials: 'same-origin' })
    if (!response.ok) throw new Error('Unable to load payload')
    return response.text()
  },
}
