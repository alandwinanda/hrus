import { useEffect, useState } from 'react'
import { fetchHealth, type HealthResponse } from './api/health'

type HealthState =
  { kind: 'loading' } | { kind: 'ok'; data: HealthResponse } | { kind: 'error'; message: string }

function App() {
  const [health, setHealth] = useState<HealthState>({ kind: 'loading' })

  useEffect(() => {
    const controller = new AbortController()
    fetchHealth(controller.signal)
      .then((data) => {
        setHealth({ kind: 'ok', data })
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        const message = error instanceof Error ? error.message : 'Gagal menghubungi API'
        setHealth({ kind: 'error', message })
      })
    return () => {
      controller.abort()
    }
  }, [])

  return (
    <main className="flex min-h-screen items-center justify-center bg-slate-50 p-6">
      <div className="w-full max-w-md rounded-2xl bg-white p-8 shadow-sm ring-1 ring-slate-200">
        <h1 className="text-2xl font-semibold text-slate-900">AI-Native HRIS</h1>
        <p className="mt-1 text-sm text-slate-500">Scaffold monorepo, belum ada fitur bisnis.</p>

        <div className="mt-6 flex items-center justify-between rounded-lg bg-slate-50 px-4 py-3">
          <span className="text-sm font-medium text-slate-700">Core API</span>
          <HealthBadge health={health} />
        </div>
      </div>
    </main>
  )
}

function HealthBadge({ health }: { health: HealthState }) {
  switch (health.kind) {
    case 'loading':
      return <span className="text-sm text-slate-400">Memeriksa…</span>
    case 'ok':
      return (
        <span className="rounded-full bg-emerald-100 px-3 py-1 text-sm font-medium text-emerald-700">
          {health.data.service}: {health.data.status}
        </span>
      )
    case 'error':
      return (
        <span className="rounded-full bg-rose-100 px-3 py-1 text-sm font-medium text-rose-700">
          Tidak terhubung ({health.message})
        </span>
      )
  }
}

export default App
