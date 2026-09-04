/**
 * Baseline sanity check for the LogSherlock UI shell.
 *
 * Its only job is to prove the theme is wired: if the canvas is near-black, the
 * panel sits a step above it, the badge is brand purple and all five severity
 * chips are distinct, then `tailwind.config.js` is being read and every token
 * resolves. Replaced by the real shell in the next step.
 */

/** The five status colours, in the order they are shown. */
const SEVERITIES = [
  {
    label: 'Error',
    hex: '#F87171',
    // Written out in full rather than composed from a variable: Tailwind
    // generates classes by scanning the source text, so `bg-severity-${key}`
    // would produce a class that exists in the markup and in no stylesheet.
    dot: 'bg-severity-error',
    chip: 'bg-severity-error/10 text-severity-error ring-severity-error/30',
  },
  {
    label: 'Warn',
    hex: '#FBBF24',
    dot: 'bg-severity-warn',
    chip: 'bg-severity-warn/10 text-severity-warn ring-severity-warn/30',
  },
  {
    label: 'Success',
    hex: '#34D399',
    dot: 'bg-severity-success',
    chip: 'bg-severity-success/10 text-severity-success ring-severity-success/30',
  },
  {
    label: 'Info',
    hex: '#38BDF8',
    dot: 'bg-severity-info',
    chip: 'bg-severity-info/10 text-severity-info ring-severity-info/30',
  },
  {
    label: 'Muted',
    hex: '#6B7280',
    dot: 'bg-severity-muted',
    chip: 'bg-severity-muted/10 text-severity-muted ring-severity-muted/30',
  },
] as const

function SeverityChip({ label, hex, dot, chip }: (typeof SEVERITIES)[number]) {
  return (
    <div
      className={`flex items-center gap-2 rounded-lg px-3 py-2 text-sm font-medium ring-1 ring-inset ${chip}`}
    >
      <span className={`h-2 w-2 shrink-0 rounded-full ${dot}`} />
      <span>{label}</span>
      <span className="ml-auto font-mono text-xs opacity-60">{hex}</span>
    </div>
  )
}

function App() {
  return (
    <div className="min-h-full bg-obsidian-950">
      <header className="border-b border-obsidian-800">
        <div className="mx-auto flex max-w-5xl items-center gap-3 px-6 py-4">
          <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-brand-purple font-mono text-sm font-bold text-white">
            LS
          </span>
          <div className="leading-tight">
            <h1 className="text-lg font-semibold tracking-tight text-slate-100">
              log-sherlock
            </h1>
            <p className="text-xs text-severity-muted">
              Multi-agent log analysis
            </p>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-5xl px-6 py-10">
        <section className="rounded-xl border border-obsidian-800 bg-obsidian-900 p-6 shadow-lg shadow-black/20">
          <h2 className="text-base font-semibold text-slate-100">
            Theme sanity check
          </h2>
          <p className="mt-1 text-sm text-severity-muted">
            This panel is <code className="text-slate-300">bg-obsidian-900</code>{' '}
            on an <code className="text-slate-300">bg-obsidian-950</code> canvas,
            divided by{' '}
            <code className="text-slate-300">border-obsidian-800</code>. If all
            three read as distinct steps of near-black, the palette is wired.
          </p>

          <div className="mt-6 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {SEVERITIES.map((severity) => (
              <SeverityChip key={severity.label} {...severity} />
            ))}
          </div>

          <div className="mt-6 flex flex-wrap items-center gap-3 border-t border-obsidian-800 pt-6">
            <button
              type="button"
              className="rounded-lg bg-brand-purple px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-brand-violet focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-purple focus-visible:ring-offset-2 focus-visible:ring-offset-obsidian-900"
            >
              Primary action
            </button>
            <span className="font-mono text-xs text-severity-muted">
              brand-purple #8B5CF6 → brand-violet #7C3AED on hover
            </span>
          </div>
        </section>
      </main>
    </div>
  )
}

export default App
