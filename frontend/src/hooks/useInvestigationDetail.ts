/**
 * One investigation's full stored report, `POST /api/investigations/{id}`.
 *
 * `id` is nullable because "nothing is selected" is a real state of the detail
 * view, and it is not the same as "the fetch failed". A `null` id issues no
 * request and reports nothing — not an empty report, and not an error.
 *
 * `loading` is derived from which request last settled — see `useHealthCheck`
 * for why that is not stored as state.
 */

import { useCallback, useEffect, useState } from 'react'

import { getInvestigationDetail, isAbortError, toApiError } from '../services/api'
import type { ApiError } from '../services/api'
import type { InvestigationDetailResponse } from '../types/api'

export interface UseInvestigationDetailResult {
  /** The selected investigation, or `null` when nothing is selected. */
  detail: InvestigationDetailResponse | null
  loading: boolean
  /** A 404 here means the record is gone — usually a stale list. */
  error: ApiError | null
  /** Re-read the current id. A no-op while nothing is selected. */
  refetch: () => void
}

interface Settled {
  key: string
  data: InvestigationDetailResponse | null
  error: ApiError | null
}

export function useInvestigationDetail(
  id: string | null,
): UseInvestigationDetailResult {
  const [token, setToken] = useState(0)
  const [settled, setSettled] = useState<Settled>({
    key: '',
    data: null,
    error: null,
  })

  // `null` while nothing is selected, which is what makes "idle" distinct from
  // "loading" below rather than a request that never resolves.
  const requestKey = id === null ? null : `${token}|${id}`

  useEffect(() => {
    if (id === null) return

    const controller = new AbortController()
    let active = true

    void (async () => {
      try {
        const data = await getInvestigationDetail(id, controller.signal)
        if (active) setSettled({ key: `${token}|${id}`, data, error: null })
      } catch (cause) {
        if (!active || isAbortError(cause)) return
        setSettled({
          key: `${token}|${id}`,
          data: null,
          error: toApiError(cause),
        })
      }
    })()

    return () => {
      active = false
      controller.abort()
    }
  }, [token, id])

  const refetch = useCallback(() => setToken((current) => current + 1), [])

  // Deselecting hides the previous report without discarding it, so selecting
  // the same id again paints immediately and refreshes in the background. Any
  // *other* id settles under a different key and reports `loading` normally.
  const selected = requestKey !== null

  return {
    detail: selected ? settled.data : null,
    loading: selected && settled.key !== requestKey,
    error: selected ? settled.error : null,
    refetch,
  }
}
