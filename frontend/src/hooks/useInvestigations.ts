/**
 * The paginated record list, `POST /api/investigations`.
 *
 * Owns the page cursor as well as the data, because the two are one thing: a
 * component that held `page` itself would have to remember to refetch after
 * every change, and would get it wrong exactly once.
 *
 * `loading` is derived from which request last settled — see
 * `useHealthCheck` for why that is not stored as state.
 */

import { useCallback, useEffect, useState } from 'react'

import {
  DEFAULT_LIMIT,
  DEFAULT_PAGE,
  isAbortError,
  listInvestigations,
  toApiError,
} from '../services/api'
import type { ApiError } from '../services/api'
import type { PaginatedInvestigationsResponse } from '../types/api'

export interface UseInvestigationsOptions {
  initialPage?: number
  /** Rows per page. Fixed for the hook's lifetime; 1-100, or the backend 422s. */
  limit?: number
}

export interface UseInvestigationsResult {
  page: number
  limit: number
  /** Move the cursor. The list refetches on change. */
  setPage: (page: number) => void
  data: PaginatedInvestigationsResponse | null
  loading: boolean
  error: ApiError | null
  /** Re-read the current page. */
  refetch: () => void
}

interface Settled {
  key: string
  data: PaginatedInvestigationsResponse | null
  error: ApiError | null
}

export function useInvestigations(
  options: UseInvestigationsOptions = {},
): UseInvestigationsResult {
  const { initialPage = DEFAULT_PAGE, limit = DEFAULT_LIMIT } = options

  const [page, setPage] = useState(initialPage)
  const [token, setToken] = useState(0)
  const [settled, setSettled] = useState<Settled>({
    key: '',
    data: null,
    error: null,
  })

  // Identifies the request this render wants. Empty string is unreachable as a
  // real key, so the first render reports `loading`.
  const requestKey = `${token}|${page}|${limit}`

  useEffect(() => {
    const controller = new AbortController()
    let active = true

    void (async () => {
      try {
        const data = await listInvestigations(page, limit, controller.signal)
        if (active) setSettled({ key: requestKey, data, error: null })
      } catch (cause) {
        if (!active || isAbortError(cause)) return
        // The previous page is dropped rather than left on screen: stale rows
        // under a fresh error read as the answer to the request that failed.
        setSettled({ key: requestKey, data: null, error: toApiError(cause) })
      }
    })()

    return () => {
      active = false
      controller.abort()
    }
  }, [requestKey, page, limit])

  const refetch = useCallback(() => setToken((current) => current + 1), [])

  return {
    page,
    limit,
    setPage,
    data: settled.data,
    loading: settled.key !== requestKey,
    error: settled.error,
    refetch,
  }
}
