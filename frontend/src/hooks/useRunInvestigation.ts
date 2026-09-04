/**
 * Running one investigation, `POST /api/investigate`.
 *
 * Manual only — nothing here fires on mount, because this is the one call that
 * spends money and minutes. The backend's own deadline is 900 seconds by
 * default, so `loading` is a state the UI has to be able to sit in for a long
 * time rather than a flicker.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import { isAbortError, runInvestigation, toApiError } from '../services/api'
import type { ApiError } from '../services/api'
import type { InvestigateRequest, InvestigateResponse } from '../types/api'

export interface UseRunInvestigationResult {
  /**
   * Start a run. Resolves with the response, or with `null` when the request
   * failed (see `error`) or was superseded — so a caller can branch on the
   * result without a `try`/`catch` around every click handler.
   *
   * Note that a resolved `InvestigateResponse` with `db_persisted: false` is a
   * *success*: the analysis ran and only its storage failed, and
   * `investigation_notes` carries the reason.
   */
  execute: (payload: InvestigateRequest) => Promise<InvestigateResponse | null>
  data: InvestigateResponse | null
  loading: boolean
  error: ApiError | null
  /** Clear the result and cancel anything in flight. */
  reset: () => void
}

export function useRunInvestigation(): UseRunInvestigationResult {
  const [data, setData] = useState<InvestigateResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<ApiError | null>(null)

  const inFlight = useRef<AbortController | null>(null)

  const execute = useCallback(
    async (payload: InvestigateRequest): Promise<InvestigateResponse | null> => {
      inFlight.current?.abort()
      const controller = new AbortController()
      inFlight.current = controller

      setLoading(true)
      setError(null)
      setData(null)
      try {
        const result = await runInvestigation(payload, controller.signal)
        if (controller.signal.aborted) return null
        setData(result)
        return result
      } catch (cause) {
        if (isAbortError(cause)) return null
        setError(toApiError(cause))
        return null
      } finally {
        if (!controller.signal.aborted) setLoading(false)
      }
    },
    [],
  )

  const reset = useCallback(() => {
    inFlight.current?.abort()
    inFlight.current = null
    setData(null)
    setError(null)
    setLoading(false)
  }, [])

  // Abandon an in-flight run when the component goes away. Only the client
  // stops listening — the graph keeps running server-side and still stores its
  // report, which is why the response carries the id rather than the UI
  // inventing one.
  useEffect(() => () => inFlight.current?.abort(), [])

  return { execute, data, loading, error, reset }
}
