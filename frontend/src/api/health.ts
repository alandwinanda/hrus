import type { components } from './schema'

export type HealthResponse = components['schemas']['HealthResponse']

export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const response = await fetch('/api/health', { signal })
  if (!response.ok) {
    throw new Error(`HTTP ${String(response.status)}`)
  }
  return (await response.json()) as HealthResponse
}
