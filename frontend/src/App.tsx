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
 * renders it and the layout branches on it — and it owns the selected row for
 * the same reason the detail panel will need it in the next step.
 */

import { useState } from 'react'

import { Header } from './components/common/Header'
import { Spinner } from './components/common/Spinner'
import { InvestigationForm } from './components/investigation/InvestigationForm'
import { InvestigationHistoryTable } from './components/investigation/InvestigationHistoryTable'
import { useInvestigations } from './hooks/useInvestigations'
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

function App() {
  const history = useInvestigations({ initialPage: 1, limit: 10 })
  const [selectedId, setSelectedId] = useState<string | null>(null)

  const handleCompleted = (result: InvestigateResponse) => {
    // Only a stored run changes what the history holds. An unstored one is
    // still a complete analysis, but refetching for it would redraw the same
    // rows and imply the record had landed.
    if (!result.db_persisted) return

    setSelectedId(result.investigation_id)
    // The new record is the newest, so it is on page one. Moving the cursor is
    // itself a refetch; asking for both would issue two requests for one page.
    if (history.page !== 1) history.setPage(1)
    else history.refetch()
  }

  const table = (
    <InvestigationHistoryTable
      data={history.data}
      loading={history.loading}
      error={history.error}
      page={history.page}
      onPageChange={history.setPage}
      onRefresh={history.refetch}
      selectedId={selectedId}
      onSelectRow={setSelectedId}
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
             list scrolls, so submitting never means scrolling back up. */
          <div className="grid grid-cols-1 gap-6 lg:grid-cols-12">
            <div className="lg:col-span-5 lg:sticky lg:top-24 lg:self-start xl:col-span-4">
              <InvestigationForm onCompleted={handleCompleted} />
            </div>
            <div className="lg:col-span-7 xl:col-span-8">{table}</div>
          </div>
        )}
      </main>
    </div>
  )
}

export default App
