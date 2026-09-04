/**
 * `GET /api/health`, fetched on mount and on demand.
 *
 * Runs immediately because a health check whose answer arrives only after a
 * click is a health check nobody reads — the point is for the shell to know
 * whether the backend is up before the user asks it to do anything.
 *
 * `loading` is *derived* rather than stored: one piece of state records the
 * request that last settled, and a request is in flight exactly when that no
 * longer matches the one the render wants. Writing it as state instead would
 * mean setting it synchronously inside the effect, which starts a second render
 * before the first has painted.
 */

import { useCallback, useEffect, useState } from 'react'

import { checkHealth, isAbortError, toApiError } from '../services/api'
import type { ApiError } from '../services/api'
import type { HealthResponse } from '../types/api'

export interface UseHealthCheckResult {
  /** The last successful answer, or `null` before one arrives. */
  health: HealthResponse | null
  loading: boolean
  error: ApiError | null
  /** Re-run the check. */
  refetch: () => void
}

/** The outcome of one request, tagged with which request it was. */
interface Settled {
  token: number
  data: HealthResponse | null
  error: ApiError | null
}

export function useHealthCheck(): UseHealthCheckResult {
  // Bumped by `refetch`; the effect keys off it, so asking again is a state
  // change like any other rather than a second code path.
  const [token, setToken] = useState(0)
  const [settled, setSettled] = useState<Settled>({
    // No request has settled yet, and -1 can never be a token, so the first
    // render already reports `loading`.
    token: -1,
    data: null,
    error: null,
  })

  useEffect(() => {
    const controller = new AbortController()
    let active = true

    void (async () => {
      try {
        const data = await checkHealth(controller.signal)
        if (active) setSettled({ token, data, error: null })
      } catch (cause) {
        // A cancellation is this hook's own doing, not a failure to report.
        if (!active || isAbortError(cause)) return
        setSettled({ token, data: null, error: toApiError(cause) })
      }
    })()

    return () => {
      active = false
      controller.abort()
    }
  }, [token])

  const refetch = useCallback(() => setToken((current) => current + 1), [])

  return {
    health: settled.data,
    loading: settled.token !== token,
    error: settled.error,
    refetch,
  }
}
