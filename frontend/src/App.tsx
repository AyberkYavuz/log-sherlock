/**
 * The application shell and its two layouts.
 *
 * Which layout renders is decided by one fact — whether any investigation is
 * stored — and the decision is deliberately deferred until that fact is known:
 *
 *   * **Scenario A**, records exist: form and history side by side on a wide
 *     screen, stacked on a narrow one.
 *   * **Scenario B**, the table is empty: the form alone, centred and given the
 *     width, with an empty-state card explaining that nothing is stored yet.
 *
 * A first paint that guessed would guess wrong half the time and snap from one
 * to the other, so the very first load renders neither. An *unreachable*
 * database is not an empty one: that falls through to Scenario A, where the
 * table's error envelope carries the reason and a retry.
 *
 * `App` owns `useInvestigations` because both halves depend on it — the table
 * renders it and the layout branches on it — and it owns `selectedId` because
 * that is shared too: the table highlights the selected row and
 * `InvestigationDetailView` fetches it.
 *
 * The inspection panel sits at full width *below* both the form and the
 * history. A report is tall — five sections, a timeline of hundreds of events,
 * a 420px graph canvas — so putting it inside the table's column would leave
 * the pager stranded far below the fold, and putting it beside the form would
 * fight the form for the narrow column. Below both, it opens in reading order:
 * the row that was clicked stays visible above it, so the list never jumps out
 * from under the pointer, and the panel grows downward into empty space rather
 * than pushing the controls off screen.
 */

import { useEffect, useRef, useState } from 'react'

import { Header } from './components/common/Header'
import { Spinner } from './components/common/Spinner'
import { InvestigationDetailView } from './components/investigation/InvestigationDetailView'
import { InvestigationForm } from './components/investigation/InvestigationForm'
import { InvestigationHistoryTable } from './components/investigation/InvestigationHistoryTable'
import {
  useDeleteInvestigation,
  useInvestigations,
} from './hooks/useInvestigations'
import type { InvestigateResponse } from './types/api'

function EmptyState({ onRefresh }: { onRefresh: () => void }) {
  return (
    <section className="rounded-xl border border-dashed border-obsidian-800 bg-obsidian-900/50 px-6 py-10 text-center">
      <span className="mx-auto flex h-10 w-10 items-center justify-center rounded-lg bg-brand-purple/10 font-mono text-sm font-bold text-brand-purple">
        LS
      </span>
      <h2 className="mt-4 text-base font-semibold text-slate-100">
        No investigations stored yet
      </h2>
      <p className="mx-auto mt-1 max-w-md text-sm text-severity-muted">
        Run one above and it will appear here. A run only reaches this list once
        it has been written to PostgreSQL — the form reports it either way.
      </p>
      <button
        type="button"
        onClick={onRefresh}
        className="mt-4 rounded-lg border border-obsidian-800 px-3 py-1.5 text-xs font-medium text-slate-300 transition-colors hover:border-brand-purple/50 hover:text-white"
      >
        Refresh
      </button>
    </section>
  )
}

function FirstLoad() {
  return (
    <div className="flex items-center justify-center gap-3 py-24 text-sm text-severity-muted">
      <Spinner className="h-4 w-4 text-severity-info" />
      Loading investigations…
    </div>
  )
}

/**
 * Scroll an element into view, honouring the reader's motion preference.
 *
 * `behavior: 'smooth'` is the requested effect and the right default, but it is
 * also exactly the kind of animation `prefers-reduced-motion` exists to
 * suppress — for some readers a long smooth scroll is nauseating rather than
 * pleasant. Those readers still get taken to the panel; they get taken there
 * instantly.
 */
function scrollIntoView(element: HTMLElement | null) {
  if (!element) return
  const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  element.scrollIntoView({
    behavior: reduced ? 'auto' : 'smooth',
    block: 'start',
  })
}

function App() {
  const history = useInvestigations()
  const [selectedId, setSelectedId] = useState<string | null>(null)

  // The two scroll targets. The panel renders below the table, so opening it
  // puts it off screen on most viewports and closing it leaves the reader
  // wherever the panel used to be — neither is somewhere useful, hence both
  // refs.
  const historyRef = useRef<HTMLDivElement>(null)
  const detailRef = useRef<HTMLDivElement>(null)

  /**
   * Scroll to the panel whenever a *different* record is selected.
   *
   * An effect rather than a click handler, because the panel does not exist yet
   * at click time: `selectedId` is what mounts it, so scrolling in the handler
   * would aim at a ref that is still null. Running after commit means the
   * element is in the document and has its height.
   *
   * The dependency is `selectedId` alone, so re-selecting the same row does not
   * re-scroll, and neither does any unrelated re-render — a refetch landing
   * underneath a reader should not yank the page.
   */
  useEffect(() => {
    if (selectedId === null) return
    scrollIntoView(detailRef.current)
  }, [selectedId])

  /**
   * Close the panel and take the reader back to the list.
   *
   * The scroll is issued before the state change rather than after: the table
   * sits *above* the panel, so removing the panel cannot move it, and its
   * position is therefore already correct. Waiting for a re-render would only
   * risk scrolling to an element mid-relayout.
   */
  const closeInspection = () => {
    scrollIntoView(historyRef.current)
    setSelectedId(null)
  }

  const handleCompleted = (result: InvestigateResponse) => {
    // Only a stored run changes what the history holds. An unstored one is
    // still a complete analysis, but refetching for it would redraw the same
    // rows and imply the record had landed.
    if (!result.db_persisted) return

    setSelectedId(result.investigation_id)
    history.refetch()
  }

  /**
   * A record was removed.
   *
   * Clearing the selection is the part that matters: the panel fetches by id,
   * so leaving a deleted id selected would send it after a row that no longer
   * exists and answer with a 404 where a moment ago there was a report. Only
   * the *inspected* record clears it — deleting some other row must not close a
   * panel the reader is reading — and that case also scrolls back to the list,
   * since the thing the reader was looking at is gone.
   *
   * The page cursor needs no attention here. Paging is client-side over the
   * loaded array now, and the table clamps an out-of-range page on render, so
   * deleting the last row of the last page lands on the new last page by
   * itself.
   */
  const handleDeleted = (id: string) => {
    if (id === selectedId) closeInspection()
    history.refetch()
  }

  const deletion = useDeleteInvestigation(handleDeleted)

  const detail = (
    <div ref={detailRef}>
      <InvestigationDetailView
        investigationId={selectedId}
        onClose={closeInspection}
      />
    </div>
  )

  const table = (
    <InvestigationHistoryTable
      data={history.data}
      loading={history.loading}
      error={history.error}
      onRefresh={history.refetch}
      selectedId={selectedId}
      onSelectRow={setSelectedId}
      onDelete={(id) => void deletion.remove(id)}
      deletingId={deletion.pendingId}
      deleteError={deletion.error}
      onDismissDeleteError={deletion.clearError}
    />
  )

  // Undecided until the first request settles — see the module docstring.
  const undecided = history.data === null && history.error === null
  const isEmpty = history.data !== null && history.data.total === 0

  return (
    <div className="min-h-full bg-obsidian-950">
      <Header />

      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8 lg:py-8">
        {undecided ? (
          <FirstLoad />
        ) : isEmpty ? (
          /* Scenario B — nothing stored: the form is the whole page. */
          <div className="mx-auto max-w-2xl space-y-6">
            <InvestigationForm onCompleted={handleCompleted} />
            <EmptyState onRefresh={history.refetch} />
          </div>
        ) : (
          /* Scenario A — records exist: form beside the history on a wide
             screen, above it on a narrow one. The form sticks while a long
             list scrolls, so submitting never means scrolling back up.

             The inspection panel takes the full width below both, because a
             report is far too tall and too wide to live inside the table's
             column. It renders nothing while `selectedId` is null, so the
             layout above is untouched until a row is clicked. */
          <div className="space-y-6">
            <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
              <div className="lg:col-span-5 lg:sticky lg:top-24 lg:self-start xl:col-span-4">
                <InvestigationForm onCompleted={handleCompleted} />
              </div>
              {/* The ref goes on the column rather than inside the table, so
                  closing the panel scrolls to the top of the history card and
                  not to whichever row happened to be first. */}
              <div ref={historyRef} className="lg:col-span-7 xl:col-span-8">
                {table}
              </div>
            </div>
            {detail}
          </div>
        )}
      </main>
    </div>
  )
}

export default App
