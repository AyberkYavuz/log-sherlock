/**
 * The paginated record list, `POST /api/investigations`, and its one mutation.
 *
 * `useInvestigations` owns the page cursor as well as the data, because the two
 * are one thing: a component that held `page` itself would have to remember to
 * refetch after every change, and would get it wrong exactly once.
 *
 * `useDeleteInvestigation` sits beside it rather than inside it, and takes the
 * refetch as a callback. Folding the delete into `useInvestigations` would give
 * the list a second responsibility and would mean every consumer of the list —
 * including the layout, which only reads `total` — carried a destructive
 * method it has no use for.
 *
 * `loading` is derived from which request last settled — see
 * `useHealthCheck` for why that is not stored as state.
 */

import { useCallback, useEffect, useRef, useState } from 'react'

import {
  DEFAULT_LIMIT,
  DEFAULT_PAGE,
  deleteInvestigation,
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

export interface UseDeleteInvestigationResult {
  /**
   * Delete one record. Resolves `true` when it is gone, `false` when the call
   * failed (see `error`), so a caller can branch without a `try`/`catch`.
   */
  remove: (id: string) => Promise<boolean>
  /** The id currently being deleted, or `null`. Drives the per-row spinner. */
  pendingId: string | null
  error: ApiError | null
  /** Dismiss a failure without retrying. */
  clearError: () => void
}

/**
 * Deleting one stored investigation, `DELETE /api/investigations/{id}`.
 *
 * Manual only, and single-flight: `pendingId` is the id in flight rather than a
 * boolean, so the table can disable and spin the one row being removed instead
 * of freezing every row. A second delete while one is pending is refused rather
 * than queued — two concurrent deletes would produce two refetches racing to
 * describe the same table.
 *
 * `onDeleted` is called only on success, and is where the history refetch goes.
 * The list is not reached into from here: this hook has no idea which page is
 * displayed, and a delete that succeeded must not depend on the caller's
 * pagination state to be reported.
 *
 * Note that the request is deliberately *not* abortable from a cleanup
 * function. A delete is not idempotent server-side — repeating it is a 404 —
 * and unmounting mid-flight does not un-delete the row, so cancelling the
 * client's interest in the answer would only hide an outcome that already
 * happened. The ref guards the state writes instead.
 */
export function useDeleteInvestigation(
  onDeleted?: (id: string) => void,
): UseDeleteInvestigationResult {
  const [pendingId, setPendingId] = useState<string | null>(null)
  const [error, setError] = useState<ApiError | null>(null)

  // Whether this hook's component is still mounted. Checked before every state
  // write after the await, so a delete that lands after the panel closed does
  // not warn about setting state on an unmounted component.
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])

  // Read inside `remove` rather than captured in its closure, so `remove` stays
  // referentially stable across renders even when the caller passes an inline
  // arrow — otherwise every render would hand the table a new callback.
  //
  // Synced in an effect rather than assigned during render: a render may be
  // discarded or replayed, and a ref written during one is a side effect that
  // survives it. `remove` only ever runs from a user gesture, which is after
  // mount, so the effect has always committed by the time it is read.
  const onDeletedRef = useRef(onDeleted)
  useEffect(() => {
    onDeletedRef.current = onDeleted
  }, [onDeleted])

  const pendingRef = useRef<string | null>(null)

  const remove = useCallback(async (id: string): Promise<boolean> => {
    if (pendingRef.current !== null) return false

    pendingRef.current = id
    setPendingId(id)
    setError(null)
    try {
      await deleteInvestigation(id)
      // Fired before the pending flag clears so the row stays disabled until
      // the refetch it triggers has been requested, rather than flicking back
      // to an enabled state over a row that is already gone.
      onDeletedRef.current?.(id)
      return true
    } catch (cause) {
      if (mounted.current) setError(toApiError(cause))
      return false
    } finally {
      pendingRef.current = null
      if (mounted.current) setPendingId(null)
    }
  }, [])

  const clearError = useCallback(() => setError(null), [])

  return { remove, pendingId, error, clearError }
}
