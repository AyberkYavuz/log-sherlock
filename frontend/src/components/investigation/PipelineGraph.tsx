/**
 * The graph's execution topology, drawn from one stored report.
 *
 * **What this can and cannot know.** A stored `structured_report` does *not*
 * carry `completed_stages` — that channel lives in graph state and never
 * reaches the database — so this view cannot replay which nodes ran. What it
 * can do is *infer* each stage's outcome from the two things the report does
 * preserve: whether the artifact that stage owns is present and intact, and
 * what that stage recorded about itself in `synthesis.investigation_notes`.
 * Every status below is therefore evidence-based rather than reported, and the
 * inspector names the evidence so a reader can disagree with it.
 *
 * That distinction is why an absent artifact is red rather than grey. Each
 * analysis stage is the sole writer of exactly one report section, and the
 * fan-in runs only once every branch has landed, so a missing section means
 * something genuinely went wrong — not merely that we cannot see it.
 *
 * **Three tiers, and the line between them.** Emerald is "ran and published
 * its artifact intact". Amber is "ran and published, but degraded" — an LLM
 * pass that fell back to arithmetic, a data-quality caveat, a root cause the
 * model declined to name. Red is "the artifact is missing". The amber tier is
 * the one that earns its keep: every LLM node in this pipeline degrades rather
 * than raises, so a degraded run and a healthy one publish an identical shape
 * and are indistinguishable from the report's structure alone. Amber is the
 * only place that difference surfaces.
 *
 * **`web_search` is deliberately absent.** It is the one node whose execution
 * leaves no trace in a stored report: `search_context` and `search_queries` are
 * working state, and the node writes no report section. Drawing it would mean
 * drawing a status this component has no evidence for, so the seven stages
 * below are exactly the seven it can speak to.
 */

import { useCallback, useMemo, useState } from 'react'
import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'

import '@xyflow/react/dist/style.css'

import type { StructuredInvestigationReport } from '../../types/api'

/** The stages this component can derive a status for. */
type StageId =
  | 'parser'
  | 'error_analysis'
  | 'statistics'
  | 'timeline'
  | 'pattern_analysis'
  | 'prepare_output'
  | 'write_to_db'

/** Emerald / amber / red, in the vocabulary the rest of the UI already uses. */
type StageStatus = 'ok' | 'warning' | 'error'

interface StageState {
  status: StageStatus
  /** One line naming the evidence this status was inferred from. */
  reason: string
  /** The notes this stage wrote about itself, verbatim. */
  notes: string[]
}

/**
 * The palette, keyed by status.
 *
 * Hex values rather than Tailwind classes because React Flow renders node
 * borders and edge strokes through inline styles and SVG attributes, which
 * cannot take a class. These are the same three values as
 * `severity.success` / `severity.warn` / `severity.error` in
 * `tailwind.config.js`, and they are the colours the specification names.
 */
const STATUS_COLOR: Record<StageStatus, string> = {
  ok: '#34D399',
  warning: '#FBBF24',
  error: '#F87171',
}

const STATUS_LABEL: Record<StageStatus, string> = {
  ok: 'Completed',
  warning: 'Degraded',
  error: 'Artifact missing',
}

/** Display name and the report section each stage owns. */
const STAGE_META: Record<StageId, { label: string; owns: string }> = {
  parser: { label: 'parser', owns: 'metadata.parser_metrics' },
  error_analysis: {
    label: 'error_analysis',
    owns: 'ai_insights.error_summary',
  },
  statistics: {
    label: 'statistics',
    owns: 'deterministic_outputs.statistics',
  },
  timeline: { label: 'timeline', owns: 'deterministic_outputs.timeline' },
  pattern_analysis: {
    label: 'pattern_analysis',
    owns: 'ai_insights.pattern_summary',
  },
  prepare_output: { label: 'prepare_output', owns: 'synthesis' },
  write_to_db: { label: 'write_to_db', owns: 'the stored row itself' },
}

/**
 * Which stage wrote a given note, matched on the prefixes the nodes emit.
 *
 * The nodes prefix their own notes — `"Parser: "`, `"Error analysis: "`,
 * `"Timeline: "` — which is what makes this attribution possible at all. Two
 * lines break that convention and are matched literally: the timeline node's
 * `"Data Quality Warning: "` (it is about excluded timestamps, which is the
 * timeline's own scope) and the write node's success line, which reads
 * `"Successfully persisted investigation …"` with no prefix.
 *
 * Ordered most-specific first: `"Error analysis skipped:"` has to be tested
 * before the bare `"Error analysis"` stem would otherwise swallow it, and both
 * before any generic fallback.
 */
const NOTE_PREFIXES: { stage: StageId; matches: string[] }[] = [
  { stage: 'parser', matches: ['parser:'] },
  {
    stage: 'error_analysis',
    matches: ['error analysis skipped:', 'error analysis:'],
  },
  { stage: 'statistics', matches: ['statistics:'] },
  { stage: 'timeline', matches: ['timeline:', 'data quality warning:'] },
  { stage: 'pattern_analysis', matches: ['pattern analysis:'] },
  { stage: 'prepare_output', matches: ['prepare output:'] },
  {
    stage: 'write_to_db',
    matches: ['write to db:', 'successfully persisted'],
  },
]

/** Group the run's notes by the stage that wrote them. */
function groupNotesByStage(notes: string[]): Record<StageId, string[]> {
  const grouped = {
    parser: [] as string[],
    error_analysis: [] as string[],
    statistics: [] as string[],
    timeline: [] as string[],
    pattern_analysis: [] as string[],
    prepare_output: [] as string[],
    write_to_db: [] as string[],
  }

  for (const note of notes) {
    const lower = note.trim().toLowerCase()
    const owner = NOTE_PREFIXES.find((candidate) =>
      candidate.matches.some((prefix) => lower.startsWith(prefix)),
    )
    // A note matching no prefix is left unattributed rather than guessed at.
    // The Investigation Notes panel shows every note regardless, so nothing is
    // lost by this component declining to place one.
    if (owner) grouped[owner.stage].push(note)
  }

  return grouped
}

/**
 * The wording each LLM node uses when its call did not land.
 *
 * Matched as a substring because the nodes append the provider's own exception
 * text after it. This is the *only* signal that separates a degraded run from a
 * healthy one — the schemas are identical either way.
 */
const LLM_UNAVAILABLE = 'llm reasoning unavailable'
const SYNTHESIS_UNAVAILABLE = 'llm synthesis unavailable'

/** The first words of `prepare_output`'s `FALLBACK_ROOT_CAUSE`. */
const FALLBACK_ROOT_CAUSE_STEM = 'root cause undetermined'

/** The first words of the pattern node's arithmetic fallback narrative. */
const DETERMINISTIC_SYNTHESIS_STEM = 'deterministic summary'

/** `parser_confidence` at or above this is not penalized by the score engine. */
const PARSER_CONFIDENCE_FLOOR = 0.8

function hasNote(notes: string[], fragment: string): boolean {
  return notes.some((note) => note.toLowerCase().includes(fragment))
}

/**
 * Infer every stage's outcome from the report.
 *
 * Each branch reads only the artifact its stage owns plus that stage's own
 * notes, which keeps the inference auditable: no stage's status depends on
 * another's, so one wrong guess cannot cascade.
 */
function deriveStageStates(
  report: StructuredInvestigationReport,
): Record<StageId, StageState> {
  const notes = report.synthesis?.investigation_notes ?? []
  const byStage = groupNotesByStage(notes)

  const metrics = report.metadata?.parser_metrics
  const statistics = report.deterministic_outputs?.statistics
  const timeline = report.deterministic_outputs?.timeline
  const errorSummary = report.ai_insights?.error_summary
  const patternSummary = report.ai_insights?.pattern_summary
  const synthesis = report.synthesis

  // -- parser -------------------------------------------------------------
  // Malformed lines and missing timestamps are the two things that cost the
  // confidence score a ratio penalty, so they are exactly what "degraded"
  // means here. Low detection confidence is the third, and it is the one that
  // says the *format* was a guess.
  let parser: StageState
  if (!metrics) {
    parser = {
      status: 'error',
      reason: 'No parser_metrics in the report — ingestion health is unknown.',
      notes: byStage.parser,
    }
  } else {
    const problems: string[] = []
    if (metrics.malformed_lines > 0) {
      problems.push(
        `${metrics.malformed_lines.toLocaleString()} of ` +
          `${metrics.total_lines.toLocaleString()} lines were malformed`,
      )
    }
    if (metrics.missing_timestamp_lines > 0) {
      problems.push(
        `${metrics.missing_timestamp_lines.toLocaleString()} entries carried no timestamp`,
      )
    }
    if (metrics.parser_confidence < PARSER_CONFIDENCE_FLOOR) {
      problems.push(
        `format detection scored only ${metrics.parser_confidence.toFixed(2)}`,
      )
    }
    parser = problems.length
      ? { status: 'warning', reason: `${problems.join('; ')}.`, notes: byStage.parser }
      : {
          status: 'ok',
          reason:
            `Parsed all ${metrics.parsed_lines.toLocaleString()} lines as ` +
            `'${metrics.detected_format}' with no malformed lines and no ` +
            'missing timestamps.',
          notes: byStage.parser,
        }
  }

  // -- statistics ---------------------------------------------------------
  // Purely deterministic, so there is no degraded tier: it either published
  // its distributions or it did not.
  const statistics_: StageState = statistics
    ? {
        status: 'ok',
        reason:
          `Published ${statistics.level_distribution?.length ?? 0} level and ` +
          `${statistics.logger_distribution?.length ?? 0} logger ` +
          `distribution rows over ` +
          `${Object.keys(statistics.metadata_distributions ?? {}).length} ` +
          'metadata keys.',
        notes: byStage.statistics,
      }
    : {
        status: 'error',
        reason: 'No statistics section in the report.',
        notes: byStage.statistics,
      }

  // -- timeline -----------------------------------------------------------
  // An empty series is not a failure: it is what the node publishes when
  // nothing in the payload could be placed on a time axis, and it says so in a
  // note. That is degraded rather than broken, because the arithmetic ran.
  let timeline_: StageState
  if (!timeline) {
    timeline_ = {
      status: 'error',
      reason: 'No timeline section in the report.',
      notes: byStage.timeline,
    }
  } else if (timeline.length === 0) {
    timeline_ = {
      status: 'warning',
      reason: 'The timeline is empty — nothing could be placed on a time axis.',
      notes: byStage.timeline,
    }
  } else if (hasNote(byStage.timeline, 'data quality warning')) {
    timeline_ = {
      status: 'warning',
      reason:
        `Published ${timeline.length} events, but some entries were excluded ` +
        'for want of a complete timestamp.',
      notes: byStage.timeline,
    }
  } else {
    const milestones = timeline.filter(
      (event) => event.event_type === 'milestone',
    ).length
    timeline_ = {
      status: 'ok',
      reason:
        `Published ${timeline.length} events — ${milestones} milestones and ` +
        `${timeline.length - milestones} populated buckets.`,
      notes: byStage.timeline,
    }
  }

  // -- error_analysis -----------------------------------------------------
  // Three distinct degraded shapes, and they mean different things: the LLM
  // pass failed, there was nothing to analyse, or the model analysed the batch
  // and declined to name a cause. The last is the most easily misread as
  // success, which is why it is called out explicitly — it is also the single
  // largest penalty against the confidence score.
  let errorAnalysis: StageState
  if (!errorSummary) {
    errorAnalysis = {
      status: 'error',
      reason: 'No error_summary section in the report.',
      notes: byStage.error_analysis,
    }
  } else if (hasNote(byStage.error_analysis, LLM_UNAVAILABLE)) {
    errorAnalysis = {
      status: 'warning',
      reason:
        `Fingerprinted ${errorSummary.unique_signatures_found} signatures, but ` +
        'the reasoning pass could not reach a model — counts and templates are ' +
        'exact, the interpretation is missing.',
      notes: byStage.error_analysis,
    }
  } else if (hasNote(byStage.error_analysis, 'skipped')) {
    errorAnalysis = {
      status: 'warning',
      reason:
        'Skipped: the payload carried no error- or warning-level entries to ' +
        'analyse.',
      notes: byStage.error_analysis,
    }
  } else if (!errorSummary.primary_error_signature_id) {
    errorAnalysis = {
      status: 'warning',
      reason:
        `Analysed ${errorSummary.total_errors_analyzed.toLocaleString()} entries ` +
        `into ${errorSummary.unique_signatures_found} signatures but named no ` +
        'primary cause.',
      notes: byStage.error_analysis,
    }
  } else {
    errorAnalysis = {
      status: 'ok',
      reason:
        `Collapsed ${errorSummary.total_errors_analyzed.toLocaleString()} entries ` +
        `into ${errorSummary.unique_signatures_found} signatures and nominated ` +
        `${errorSummary.primary_error_signature_id}.`,
      notes: byStage.error_analysis,
    }
  }

  // -- pattern_analysis ---------------------------------------------------
  // This node degrades further than its siblings: it derives the same summary
  // arithmetically rather than returning nothing, so a fallback still
  // publishes a full `PatternSummary`. The narrative's opening words are the
  // only structural tell.
  let patternAnalysis: StageState
  const synthesisText = patternSummary?.behavioral_synthesis?.trim() ?? ''
  if (!patternSummary) {
    patternAnalysis = {
      status: 'error',
      reason: 'No pattern_summary section in the report.',
      notes: byStage.pattern_analysis,
    }
  } else if (
    hasNote(byStage.pattern_analysis, LLM_UNAVAILABLE) ||
    synthesisText.toLowerCase().startsWith(DETERMINISTIC_SYNTHESIS_STEM)
  ) {
    patternAnalysis = {
      status: 'warning',
      reason:
        'Fell back to the arithmetic pattern summary — the anomalies are ' +
        'derived from thresholds rather than reasoned about.',
      notes: byStage.pattern_analysis,
    }
  } else if (!synthesisText) {
    patternAnalysis = {
      status: 'warning',
      reason: 'Published no behavioral synthesis — there was nothing to narrate.',
      notes: byStage.pattern_analysis,
    }
  } else {
    patternAnalysis = {
      status: 'ok',
      reason:
        `Reported ${patternSummary.anomalies?.length ?? 0} anomalies, ` +
        `${patternSummary.cross_logger_correlations?.length ?? 0} cross-logger ` +
        `correlations and ${patternSummary.metadata_insights?.length ?? 0} ` +
        'metadata insights.',
      notes: byStage.pattern_analysis,
    }
  }

  // -- prepare_output -----------------------------------------------------
  // The fixed fallback text is worded to be unmistakably an absence rather
  // than a finding, which is exactly what makes it detectable here.
  let prepareOutput: StageState
  const rootCause = synthesis?.root_cause?.trim() ?? ''
  if (!synthesis) {
    prepareOutput = {
      status: 'error',
      reason: 'No synthesis section in the report.',
      notes: byStage.prepare_output,
    }
  } else if (
    hasNote(byStage.prepare_output, SYNTHESIS_UNAVAILABLE) ||
    rootCause.toLowerCase().startsWith(FALLBACK_ROOT_CAUSE_STEM)
  ) {
    prepareOutput = {
      status: 'warning',
      reason:
        'Published the fallback synthesis — every deterministic finding is ' +
        'intact, but no narrative was produced and the score was discounted.',
      notes: byStage.prepare_output,
    }
  } else if (!rootCause) {
    prepareOutput = {
      status: 'warning',
      reason: 'Published no root cause.',
      notes: byStage.prepare_output,
    }
  } else {
    const score = report.metadata?.confidence_score
    prepareOutput = {
      status: 'ok',
      reason:
        'Synthesized a root cause and executive summary' +
        (score === null || score === undefined
          ? '.'
          : `, scored ${score}/100.`),
      notes: byStage.prepare_output,
    }
  }

  // -- write_to_db --------------------------------------------------------
  // This one is not inferred at all, and that is worth stating: the report is
  // being read *back out of* the table, so the write demonstrably succeeded.
  // No other stage has evidence this direct.
  const writeToDb: StageState = {
    status: 'ok',
    reason:
      'Persisted — this report was read back from the investigations table.',
    notes: byStage.write_to_db,
  }

  return {
    parser,
    statistics: statistics_,
    timeline: timeline_,
    error_analysis: errorAnalysis,
    pattern_analysis: patternAnalysis,
    prepare_output: prepareOutput,
    write_to_db: writeToDb,
  }
}

/**
 * Where each stage sits on the canvas.
 *
 * Laid out left to right in execution order, with the three branches `parser`
 * fans out into stacked in the middle column. `pattern_analysis` sits between
 * the deterministic pair and the fan-in because that is where it actually runs
 * — one superstep later than the pair it reads, not beside it.
 */
const STAGE_POSITION: Record<StageId, { x: number; y: number }> = {
  parser: { x: 0, y: 170 },
  error_analysis: { x: 250, y: 0 },
  statistics: { x: 250, y: 150 },
  timeline: { x: 250, y: 280 },
  pattern_analysis: { x: 490, y: 215 },
  prepare_output: { x: 730, y: 170 },
  write_to_db: { x: 970, y: 170 },
}

/**
 * The real topology, edge for edge, as `graph.py` builds it.
 *
 * The three edges into `prepare_output` from `parser`, `statistics` and
 * `timeline` look redundant next to `pattern_analysis` and are not: each
 * analysis stage publishes its own artifact rather than forwarding its inputs,
 * so the fan-in needs the raw statistics and timeline as well as the patterns
 * derived from them, and `parser_metrics` reaches it no other way.
 *
 * `dashed` marks the two edges that are conditional or bypassable at runtime.
 * The `error_analysis → prepare_output` edge is drawn dashed because in the
 * real graph it is a conditional branch that may first detour through
 * `web_search`; a solid line would imply a directness the topology does not
 * have.
 */
const PIPELINE_EDGES: { from: StageId; to: StageId; dashed?: boolean }[] = [
  { from: 'parser', to: 'error_analysis' },
  { from: 'parser', to: 'statistics' },
  { from: 'parser', to: 'timeline' },
  { from: 'parser', to: 'prepare_output', dashed: true },
  { from: 'statistics', to: 'pattern_analysis' },
  { from: 'timeline', to: 'pattern_analysis' },
  { from: 'statistics', to: 'prepare_output', dashed: true },
  { from: 'timeline', to: 'prepare_output', dashed: true },
  { from: 'error_analysis', to: 'prepare_output', dashed: true },
  { from: 'pattern_analysis', to: 'prepare_output' },
  { from: 'prepare_output', to: 'write_to_db' },
]

/** The data a custom node carries. React Flow requires an index signature. */
interface StageNodeData extends Record<string, unknown> {
  stage: StageId
  state: StageState
  selected: boolean
}

type StageNode = Node<StageNodeData, 'stage'>

/**
 * One stage box.
 *
 * A custom node rather than a styled default, because the status colour has to
 * drive the border, the dot and the caption together — and because the default
 * node renders its label as a bare string, with nowhere to put the second line.
 */
function StageNodeBox({ data }: NodeProps<StageNode>) {
  const { stage, state, selected } = data
  const color = STATUS_COLOR[state.status]

  return (
    <div
      className="rounded-lg border-2 bg-obsidian-900 px-3 py-2 text-left shadow-lg shadow-black/40 transition-shadow"
      style={{
        borderColor: color,
        minWidth: 168,
        boxShadow: selected ? `0 0 0 3px ${color}44` : undefined,
      }}
    >
      {/* Both handles are hidden but present: React Flow needs them as edge
          anchors, and a visible dot on each side of every box adds noise to a
          diagram whose edges already read unambiguously left to right. */}
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="flex items-center gap-2">
        <span
          className="h-2 w-2 shrink-0 rounded-full"
          style={{ backgroundColor: color }}
        />
        <span className="font-mono text-xs font-semibold text-slate-100">
          {STAGE_META[stage].label}
        </span>
      </div>
      <p className="mt-0.5 text-[10px] uppercase tracking-wide" style={{ color }}>
        {STATUS_LABEL[state.status]}
      </p>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  )
}

/** Registered once, outside the component, so React Flow sees a stable object. */
const NODE_TYPES = { stage: StageNodeBox }

function LegendSwatch({ status }: { status: StageStatus }) {
  return (
    <span className="flex items-center gap-1.5">
      <span
        className="h-2 w-2 rounded-full"
        style={{ backgroundColor: STATUS_COLOR[status] }}
      />
      <span className="text-severity-muted">{STATUS_LABEL[status]}</span>
    </span>
  )
}

export function PipelineGraph({
  report,
}: {
  report: StructuredInvestigationReport
}) {
  const states = useMemo(() => deriveStageStates(report), [report])
  const [selectedStage, setSelectedStage] = useState<StageId | null>(null)

  const nodes = useMemo<StageNode[]>(
    () =>
      (Object.keys(STAGE_POSITION) as StageId[]).map((stage) => ({
        id: stage,
        type: 'stage' as const,
        position: STAGE_POSITION[stage],
        data: {
          stage,
          state: states[stage],
          selected: selectedStage === stage,
        },
      })),
    [states, selectedStage],
  )

  const edges = useMemo<Edge[]>(
    () =>
      PIPELINE_EDGES.map(({ from, to, dashed }) => ({
        id: `${from}->${to}`,
        source: from,
        target: to,
        animated: false,
        style: {
          // An edge is coloured by the stage it leaves, so a degraded stage
          // visibly taints everything downstream of it rather than looking
          // like an isolated amber box.
          stroke: STATUS_COLOR[states[from].status],
          strokeWidth: 1.5,
          strokeDasharray: dashed ? '4 3' : undefined,
          opacity: 0.75,
        },
      })),
    [states],
  )

  const handleNodeClick = useCallback(
    (_event: React.MouseEvent, node: Node) => {
      setSelectedStage((current) =>
        current === node.id ? null : (node.id as StageId),
      )
    },
    [],
  )

  const inspected = selectedStage ? states[selectedStage] : null

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs">
        <LegendSwatch status="ok" />
        <LegendSwatch status="warning" />
        <LegendSwatch status="error" />
        <p className="text-severity-muted">
          Click a stage to inspect what it recorded.
        </p>
      </div>

      <div className="h-[420px] overflow-hidden rounded-lg border border-obsidian-800 bg-obsidian-950">
        <ReactFlow<StageNode>
          nodes={nodes}
          edges={edges}
          nodeTypes={NODE_TYPES}
          onNodeClick={handleNodeClick}
          onPaneClick={() => setSelectedStage(null)}
          fitView
          fitViewOptions={{ padding: 0.15 }}
          // The topology is fixed and derived from `graph.py`, so the canvas is
          // an inspector rather than an editor: panning and zooming are useful
          // on a narrow screen, but dragging a node or drawing an edge would
          // only let a reader misrepresent the graph they are looking at.
          nodesDraggable={false}
          nodesConnectable={false}
          edgesFocusable={false}
          proOptions={{ hideAttribution: true }}
          minZoom={0.4}
          maxZoom={1.6}
        >
          <Background
            variant={BackgroundVariant.Dots}
            gap={18}
            size={1}
            color="#1F2937"
          />
          <Controls showInteractive={false} className="!bg-obsidian-900" />
        </ReactFlow>
      </div>

      {inspected && selectedStage ? (
        <div
          className="rounded-lg border p-4"
          style={{
            borderColor: `${STATUS_COLOR[inspected.status]}66`,
            backgroundColor: `${STATUS_COLOR[inspected.status]}14`,
          }}
        >
          <div className="flex flex-wrap items-baseline gap-2">
            <span className="font-mono text-sm font-semibold text-slate-100">
              {STAGE_META[selectedStage].label}
            </span>
            <span
              className="text-xs font-medium uppercase tracking-wide"
              style={{ color: STATUS_COLOR[inspected.status] }}
            >
              {STATUS_LABEL[inspected.status]}
            </span>
            <code className="ml-auto font-mono text-[11px] text-severity-muted">
              owns {STAGE_META[selectedStage].owns}
            </code>
          </div>

          <p className="mt-2 text-sm text-slate-200">{inspected.reason}</p>

          {inspected.notes.length > 0 ? (
            <ul className="mt-3 space-y-1.5 border-t border-obsidian-800 pt-3">
              {inspected.notes.map((note, index) => (
                <li
                  key={`${index}-${note.slice(0, 24)}`}
                  className="text-xs leading-relaxed text-severity-muted"
                >
                  {note}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-3 border-t border-obsidian-800 pt-3 text-xs text-severity-muted">
              This stage recorded no investigation notes. Nodes write a note
              only when there is something to report, so silence here is
              ordinary.
            </p>
          )}
        </div>
      ) : (
        <p className="rounded-lg border border-obsidian-800 bg-obsidian-900/50 px-4 py-3 text-xs leading-relaxed text-severity-muted">
          Statuses are inferred from the stored report, not replayed from the
          run: <code className="font-mono">completed_stages</code> is graph
          state and is never persisted. Each stage is judged on whether the
          artifact it owns is present and intact, and on what it recorded in the
          investigation notes.{' '}
          <code className="font-mono">web_search</code> is not drawn because it
          writes no report section, so a stored report carries no evidence of
          whether it ran.
        </p>
      )}
    </div>
  )
}
