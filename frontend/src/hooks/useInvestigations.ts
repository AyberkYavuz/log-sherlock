/**
 * The stored record list, `POST /api/investigations`, and its one mutation.
 *
 * **Why this loads every record rather than one page.** The endpoint paginates
 * server-side, but the history panel filters client-side, and a filter has to
 * see everything it claims to search. Filtering one page of ten would make
 * "no results" mean "not on the page you happen to be looking at" — a search
 * that reports absence it cannot actually establish. So the hook reads the
 * whole table once and both filtering and paging happen in the component.
 *
 * That trade is bounded rather than unlimited. `MAX_LIMIT` on the API is 100,
 * so the load is a first request that reports `total` followed by the remaining
 * pages fetched concurrently, and it stops at :data:`MAX_RECORDS`. Past that
 * ceiling `truncated` is set and the component says so, because a search over a
 * silently clipped set is the same lie in a different place.
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
  deleteInvestigation,
  isAbortError,
  listInvestigations,
  toApiError,
} from '../services/api'
import type { ApiError } from '../services/api'
import type { InvestigationItem } from '../types/api'

/**
 * Rows per request. The API's `MAX_LIMIT`; anything above it is a 422, so this
 * is the fewest round trips the whole table can be read in.
 */
export const FETCH_PAGE_SIZE = 100

/**
 * The most rows this hook will hold.
 *
 * Ten requests' worth. The ceiling exists because the client keeps every row in
 * memory and re-scans them on each keystroke of the filter; it is high enough
 * that no realistic development table reaches it, and low enough that a table
 * which has grown past client-side search cannot quietly degrade the browser.
 * Reaching it sets `truncated` rather than failing.
 */
export const MAX_RECORDS = 1000

export interface InvestigationsSnapshot {
  /** Every row loaded, newest first, unfiltered and unpaginated. */
  items: InvestigationItem[]
  /** Rows in the whole table per the server, which may exceed `items.length`. */
  total: number
  /** Whether `MAX_RECORDS` cut the load short of `total`. */
  truncated: boolean
}

export interface UseInvestigationsResult {
  /** `null` until the first load settles — see `App` for why that matters. */
  data: InvestigationsSnapshot | null
  loading: boolean
  error: ApiError | null
  /** Re-read the whole list. */
  refetch: () => void
}

interface Settled {
  token: number
  data: InvestigationsSnapshot | null
  error: ApiError | null
}

/**
 * Read the whole table, in as few requests as its size allows.
 *
 * The first page is awaited alone because it is what reports `total`; there is
 * no way to know how many requests are needed without it. The rest go out
 * together rather than in sequence, so a 400-row table costs one round trip
 * plus one, not four.
 */
async function fetchAllInvestigations(
  signal: AbortSignal,
): Promise<InvestigationsSnapshot> {
  const first = await listInvestigations(1, FETCH_PAGE_SIZE, signal)
  const total = first.total
  const capped = Math.min(total, MAX_RECORDS)

  const items = [...first.items]
  const pagesNeeded = Math.ceil(capped / FETCH_PAGE_SIZE)

  if (pagesNeeded > 1) {
    const rest = await Promise.all(
      Array.from({ length: pagesNeeded - 1 }, (_, index) =>
        listInvestigations(index + 2, FETCH_PAGE_SIZE, signal),
      ),
    )
    for (const page of rest) items.push(...page.items)
  }

  return {
    // Trimmed rather than trusted: `total` can grow between the first request
    // and the last, so the tail page may carry rows past the cap.
    items: items.slice(0, capped),
    total,
    truncated: total > capped,
  }
}

export function useInvestigations(): UseInvestigationsResult {
  const [token, setToken] = useState(0)
  const [settled, setSettled] = useState<Settled>({
    // No load has settled yet, and -1 can never be a token, so the first
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
        const data = await fetchAllInvestigations(controller.signal)
        if (active) setSettled({ token, data, error: null })
      } catch (cause) {
        if (!active || isAbortError(cause)) return
        // The previous rows are dropped rather than left on screen: stale rows
        // under a fresh error read as the answer to the request that failed.
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
    data: settled.data,
    loading: settled.token !== token,
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
