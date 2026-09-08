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
 * **Status answers one question: did this stage execute?** Emerald is "it ran
 * and published its artifact". Red is "the artifact is missing". There is no
 * amber tier, and its removal is deliberate rather than a simplification.
 *
 * A `DEGRADED` badge was previously raised on benign data observations — logs
 * that carried no timestamp, a search that found nothing above the relevance
 * floor, an LLM pass that fell back to arithmetic. In an observability UI that
 * word means an operational fault, and none of those are one: the node ran,
 * returned, and published exactly what the data supported. Amber on a healthy
 * run trains a reader to ignore amber, which costs them the one case that does
 * matter.
 *
 * **The observations did not go away — only the badge did.** Every one of those
 * findings is still computed, still worded exactly as before, and still shown:
 * the `reason` line of each stage carries it, and the inspector below the canvas
 * renders that line plus the stage's own notes verbatim. So "70 entries carried
 * no timestamp" and "nothing cleared the relevance floor" are as visible as they
 * ever were. What changed is that a reader now finds them by opening a stage
 * that says `Completed`, instead of being alarmed into it by a colour that
 * implied a fault.
 *
 * The status/observation split is the point: **status is about execution,
 * `reason` is about data.** Conflating them is what produced the misleading
 * badge in the first place.
 *
 * **`web_search` is drawn, and it is the exception that proves the rule.** It
 * writes no report *section* — `search_context` and `search_queries` are
 * working state and never reach the database — so for a long time this
 * component left it out on the grounds that a stored report carries no evidence
 * of it. That was half right. The node writes no section, but it does write
 * `investigation_notes`, every one of them prefixed `"Web search: "`, and those
 * notes are snapshotted into `synthesis` like every other node's. So the
 * evidence exists; it is simply of a different kind, and it supports a weaker
 * claim than the other seven stages'.
 *
 * That weaker claim is why `web_search` gets two tiers no other stage has:
 *
 *   * **Skipped** — the run recorded notes and none of them are the search
 *     node's. `error_analysis` pass 1 asked for no lookups (or the caller left
 *     `enable_web_search` off), so the router went straight to `prepare_output`.
 *     This is a *positive* finding about a detour not taken, which is why it is
 *     dimmed rather than red: nothing went wrong.
 *   * **Pending** — the report records no notes at all, so there is nothing to
 *     read the absence of. Not run, not skipped: undetermined. Every other
 *     stage can fall back to "is my artifact there?"; this one cannot, and
 *     inventing a status for it would be exactly the mistake the rest of this
 *     module avoids.
 *
 * The distinction matters because the two look identical from the topology and
 * mean opposite things — one is a decision the graph made, the other is our own
 * blindness.
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
  | 'web_search'
  | 'statistics'
  | 'timeline'
  | 'pattern_analysis'
  | 'prepare_output'
  | 'write_to_db'

/**
 * The four execution outcomes a stage can be in.
 *
 * Every one of them answers "did this stage run?" and nothing else. There is
 * deliberately no tier for "ran, but the data was imperfect" — see the module
 * docstring; that belongs in `reason`, not in a colour.
 *
 * `skipped` and `pending` are both "did not produce anything", and separating
 * them is the whole point: `skipped` is a decision the graph made and reported,
 * `pending` is an absence of evidence. Collapsing the two would mean rendering
 * "we don't know" as "it didn't run".
 */
type StageStatus = 'ok' | 'error' | 'skipped' | 'pending'

interface StageState {
  /** Did this stage execute? Nothing about data quality lives here. */
  status: StageStatus
  /**
   * What the evidence actually said, in one line.
   *
   * This is where every data observation lands — malformed lines, entries with
   * no timestamp, a search that cleared nothing, an LLM pass that fell back to
   * arithmetic. Two stages with the same `Completed` badge routinely carry very
   * different `reason`s, and that is the intended design rather than a loss of
   * information: the badge reports execution, this reports findings.
   */
  reason: string
  /** The notes this stage wrote about itself, verbatim. */
  notes: string[]
}

/**
 * The palette, keyed by status.
 *
 * Hex values rather than Tailwind classes because React Flow renders node
 * borders and edge strokes through inline styles and SVG attributes, which
 * cannot take a class. These match `severity.success` / `severity.error` /
 * `severity.muted` / `severity.info` in `tailwind.config.js`.
 *
 * `severity.warn` (amber) is pointedly absent. It is still the right colour for
 * a data-quality *note* — `StructuredReportView` uses it for exactly that — but
 * no longer for a stage's execution status.
 */
const STATUS_COLOR: Record<StageStatus, string> = {
  ok: '#34D399',
  error: '#F87171',
  // `severity.muted` — the colour this UI already uses for "not measured" on a
  // score badge, which is the same kind of statement.
  skipped: '#6B7280',
  // `severity.info` — the same blue as the header's "Checking…" badge, which is
  // also an undetermined-yet state rather than a bad one.
  pending: '#38BDF8',
}

const STATUS_LABEL: Record<StageStatus, string> = {
  ok: 'Completed',
  error: 'Artifact missing',
  skipped: 'Skipped',
  pending: 'Undetermined',
}

/**
 * Statuses that mean "this stage did not run", and are drawn back rather than
 * flagged. Both dim the node and its outgoing edges: a bypassed detour should
 * read as inactive at a glance, not compete with the stages that did work.
 */
const INACTIVE_STATUSES: ReadonlySet<StageStatus> = new Set<StageStatus>([
  'skipped',
  'pending',
])

/** Display name and the report section each stage owns. */
const STAGE_META: Record<StageId, { label: string; owns: string }> = {
  parser: { label: 'parser', owns: 'metadata.parser_metrics' },
  error_analysis: {
    label: 'error_analysis',
    owns: 'ai_insights.error_summary',
  },
  // The only stage that owns no report section. Naming that in the inspector
  // rather than leaving the field blank is what keeps its weaker evidence
  // visible instead of implicit.
  web_search: {
    label: 'web_search',
    owns: 'no report section — investigation notes only',
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
  { stage: 'web_search', matches: ['web search:'] },
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
    web_search: [] as string[],
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
 * text after it. This is the *only* signal that a node fell back to arithmetic
 * rather than reaching its model — the schemas are identical either way, so
 * without this the two are indistinguishable. It no longer changes the stage's
 * status, which is `Completed` either way; it changes what `reason` says.
 */
const LLM_UNAVAILABLE = 'llm reasoning unavailable'
const SYNTHESIS_UNAVAILABLE = 'llm synthesis unavailable'

/**
 * The search node's own wordings, from `graph_library/web_search/`.
 *
 * `unavailable` is written by the node when `run_web_search` raised — no
 * `TAVILY_API_KEY`, no `tavily-python`, no network. The summary line is written
 * on every successful pass and is the only note that carries counts, which is
 * why the two numbers are pulled out of it by pattern rather than by field.
 */
const SEARCH_UNAVAILABLE = 'web search: unavailable'
const SEARCH_RAN = /ran (\d+) quer(?:y|ies) and retrieved (\d+) relevant/i

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
  // Malformed lines, missing timestamps and low detection confidence are the
  // three things that cost the confidence score a penalty, so they are what is
  // worth reporting about a parse. None of them is a parser *failure* — the
  // node read what it was given and said what it found — so they shape `reason`
  // and leave the status at `Completed`.
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
      ? { status: 'ok', reason: `${problems.join('; ')}.`, notes: byStage.parser }
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
  // Purely deterministic and binary: it either published its distributions or
  // it did not.
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
  // note. The arithmetic ran, so the stage completed — the emptiness, and any
  // entries excluded for want of a usable timestamp, are reported in `reason`.
  let timeline_: StageState
  if (!timeline) {
    timeline_ = {
      status: 'error',
      reason: 'No timeline section in the report.',
      notes: byStage.timeline,
    }
  } else if (timeline.length === 0) {
    timeline_ = {
      status: 'ok',
      reason: 'The timeline is empty — nothing could be placed on a time axis.',
      notes: byStage.timeline,
    }
  } else if (hasNote(byStage.timeline, 'data quality warning')) {
    timeline_ = {
      status: 'ok',
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
  // Four outcomes that all count as having run, and they mean very different
  // things: the LLM pass could not reach a model, there was nothing to analyse,
  // the model analysed the batch and declined to name a cause, or it named one.
  // The third is the most easily misread as the fourth, which is why `reason`
  // states it outright — it is also the single largest penalty against the
  // confidence score.
  let errorAnalysis: StageState
  if (!errorSummary) {
    errorAnalysis = {
      status: 'error',
      reason: 'No error_summary section in the report.',
      notes: byStage.error_analysis,
    }
  } else if (hasNote(byStage.error_analysis, LLM_UNAVAILABLE)) {
    errorAnalysis = {
      status: 'ok',
      reason:
        `Fingerprinted ${errorSummary.unique_signatures_found} signatures, but ` +
        'the reasoning pass could not reach a model — counts and templates are ' +
        'exact, the interpretation is missing.',
      notes: byStage.error_analysis,
    }
  } else if (hasNote(byStage.error_analysis, 'skipped')) {
    // The node's own note says "skipped", but it is not the `skipped` *status*:
    // that is reserved for a stage the router bypassed, and this one ran and
    // published an `error_summary`. It simply had nothing to fingerprint.
    errorAnalysis = {
      status: 'ok',
      reason:
        'Ran with nothing to fingerprint — the payload carried no error- or ' +
        'warning-level entries to analyse.',
      notes: byStage.error_analysis,
    }
  } else if (!errorSummary.primary_error_signature_id) {
    errorAnalysis = {
      status: 'ok',
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

  // -- web_search ---------------------------------------------------------
  // The conditional detour, and the only stage judged purely on notes. The
  // order of these branches is the order of decreasing certainty: it said it
  // failed, it said it ran, the run kept notes and said neither, we have no
  // notes to read at all.
  let webSearch: StageState
  const searchNotes = byStage.web_search
  const ranNote = searchNotes.find((note) => SEARCH_RAN.test(note))
  const ranMatch = ranNote?.match(SEARCH_RAN)

  if (hasNote(searchNotes, SEARCH_UNAVAILABLE)) {
    webSearch = {
      status: 'ok',
      reason:
        'Ran, but the search backend could not be reached — no API key, no ' +
        'SDK, or no network. The analysis continued without external ' +
        'documentation.',
      notes: searchNotes,
    }
  } else if (ranMatch) {
    const queries = Number(ranMatch[1])
    const snippets = Number(ranMatch[2])
    const partialFailure = hasNote(searchNotes, 'failed')

    if (snippets === 0) {
      webSearch = {
        status: 'ok',
        reason:
          `Ran ${queries} ${queries === 1 ? 'query' : 'queries'} but nothing ` +
          'cleared the relevance floor, so the second error-analysis pass saw ' +
          'no external context.',
        notes: searchNotes,
      }
    } else if (partialFailure) {
      webSearch = {
        status: 'ok',
        reason:
          `Retrieved ${snippets} ${snippets === 1 ? 'snippet' : 'snippets'} ` +
          `from ${queries} ${queries === 1 ? 'query' : 'queries'}, but at ` +
          'least one query failed outright — the context is partial.',
        notes: searchNotes,
      }
    } else {
      webSearch = {
        status: 'ok',
        reason:
          `Ran ${queries} ${queries === 1 ? 'query' : 'queries'} and fed ` +
          `${snippets} relevant ${snippets === 1 ? 'snippet' : 'snippets'} ` +
          'back into the second error-analysis pass.',
        notes: searchNotes,
      }
    }
  } else if (searchNotes.length > 0) {
    // Notes from this node but no recognized summary — an older release, or a
    // wording that has since changed. Reporting the uncertainty beats guessing.
    webSearch = {
      status: 'ok',
      reason:
        'Recorded notes, but none of them state the outcome in a form this ' +
        'view recognizes.',
      notes: searchNotes,
    }
  } else if (notes.length === 0) {
    // Nothing to read the absence of. See the module docstring.
    webSearch = {
      status: 'pending',
      reason:
        'Undetermined. This report records no investigation notes at all, so ' +
        'there is no evidence either way — unlike the other stages, this one ' +
        'owns no report section to fall back on.',
      notes: searchNotes,
    }
  } else {
    webSearch = {
      status: 'skipped',
      reason:
        'Bypassed. The run recorded notes and none are the search node’s, ' +
        'so error_analysis asked for no lookups and the router went straight ' +
        'to prepare_output. Nothing went wrong — the detour was not needed.',
      notes: searchNotes,
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
      status: 'ok',
      reason:
        'Fell back to the arithmetic pattern summary — the anomalies are ' +
        'derived from thresholds rather than reasoned about.',
      notes: byStage.pattern_analysis,
    }
  } else if (!synthesisText) {
    patternAnalysis = {
      status: 'ok',
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
      status: 'ok',
      reason:
        'Published the fallback synthesis — every deterministic finding is ' +
        'intact, but no narrative was produced and the score was discounted.',
      notes: byStage.prepare_output,
    }
  } else if (!rootCause) {
    prepareOutput = {
      status: 'ok',
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
    web_search: webSearch,
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
 *
 * `web_search` sits directly *above* `error_analysis` rather than after it,
 * which is the only placement that tells the truth about the topology: it is
 * not a step between error analysis and the fan-in, it is a detour off
 * `error_analysis` that loops straight back into it. Putting it in the flow
 * would draw a stage every run passes through, when most runs never enter it at
 * all.
 */
const STAGE_POSITION: Record<StageId, { x: number; y: number }> = {
  parser: { x: 0, y: 245 },
  web_search: { x: 250, y: 0 },
  error_analysis: { x: 250, y: 110 },
  statistics: { x: 250, y: 250 },
  timeline: { x: 250, y: 380 },
  pattern_analysis: { x: 490, y: 315 },
  prepare_output: { x: 730, y: 245 },
  write_to_db: { x: 970, y: 245 },
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
 * `dashed` marks the edges that are conditional or bypassable at runtime. The
 * `error_analysis → prepare_output` edge is drawn dashed because in the real
 * graph it is a conditional branch that may first detour through `web_search`;
 * a solid line would imply a directness the topology does not have.
 *
 * The two `web_search` edges are the conditional loop `graph.py` builds with
 * `add_conditional_edges("error_analysis", …)` and
 * `add_edge("web_search", "error_analysis")`. Both are drawn, in both
 * directions, because a single arrow into `web_search` would leave a reader
 * asking where its output goes — and the answer, that it goes back into the
 * stage it came from for a second pass, is the most surprising thing about this
 * graph. `label` is carried only by these two, since they are the only edges
 * whose meaning is not obvious from the boxes they join.
 *
 * They anchor on the vertical handles and are horizontally offset from one
 * another, so the outbound and return legs read as two lines rather than
 * overlapping into one ambiguous stroke.
 */
interface PipelineEdge {
  from: StageId
  to: StageId
  dashed?: boolean
  label?: string
  sourceHandle?: string
  targetHandle?: string
}

const PIPELINE_EDGES: PipelineEdge[] = [
  { from: 'parser', to: 'error_analysis' },
  { from: 'parser', to: 'statistics' },
  { from: 'parser', to: 'timeline' },
  { from: 'parser', to: 'prepare_output', dashed: true },
  {
    from: 'error_analysis',
    to: 'web_search',
    dashed: true,
    label: 'needs research',
    sourceHandle: 'up-out',
    targetHandle: 'down-in',
  },
  {
    from: 'web_search',
    to: 'error_analysis',
    dashed: true,
    label: 'pass 2',
    sourceHandle: 'down-out',
    targetHandle: 'up-in',
  },
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
  const inactive = INACTIVE_STATUSES.has(state.status)

  return (
    <div
      className="rounded-lg bg-obsidian-900 px-3 py-2 text-left shadow-lg shadow-black/40 transition-shadow"
      style={{
        borderColor: color,
        borderWidth: 2,
        // A dashed outline at reduced opacity is what separates "did not run"
        // from "ran and failed" without spending a fourth colour on it. The
        // box is still legible and still clickable — it is drawn back, not
        // disabled.
        borderStyle: inactive ? 'dashed' : 'solid',
        opacity: inactive ? 0.55 : 1,
        minWidth: 168,
        boxShadow: selected ? `0 0 0 3px ${color}44` : undefined,
      }}
    >
      {/* Every handle is hidden but present: React Flow needs them as edge
          anchors, and a visible dot on each side of every box adds noise to a
          diagram whose edges already read unambiguously left to right.

          All four exist on every node even though only the `error_analysis`
          <-> `web_search` loop uses the vertical pair. Giving one node a
          different handle set would mean an edge silently failing to render
          the day another conditional branch is added, and an invisible handle
          costs nothing. The two vertical handles on each edge are offset to
          opposite thirds so the loop's legs do not overlap. */}
      <Handle
        type="target"
        id="up-in"
        position={Position.Top}
        style={{ opacity: 0, left: '35%' }}
      />
      <Handle
        type="source"
        id="up-out"
        position={Position.Top}
        style={{ opacity: 0, left: '65%' }}
      />
      <Handle
        type="target"
        id="in"
        position={Position.Left}
        style={{ opacity: 0 }}
      />
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
      <Handle
        type="source"
        id="out"
        position={Position.Right}
        style={{ opacity: 0 }}
      />
      <Handle
        type="target"
        id="down-in"
        position={Position.Bottom}
        style={{ opacity: 0, left: '35%' }}
      />
      <Handle
        type="source"
        id="down-out"
        position={Position.Bottom}
        style={{ opacity: 0, left: '65%' }}
      />
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
      PIPELINE_EDGES.map(
        ({ from, to, dashed, label, sourceHandle, targetHandle }) => {
          // An edge is coloured by the stage it leaves, so a stage whose
          // artifact is missing visibly taints everything downstream of it
          // rather than looking like one isolated red box.
          const status = states[from].status
          // Either end being inactive dims the edge. Colouring by the source
          // alone is right for the outbound leg, but the return leg leaves
          // `web_search` and would otherwise be drawn at full strength into a
          // bypassed box.
          const faded =
            INACTIVE_STATUSES.has(status) ||
            INACTIVE_STATUSES.has(states[to].status)

          return {
            id: `${from}->${to}`,
            source: from,
            target: to,
            // Named explicitly rather than left undefined. Every node carries
            // six handles now, and an edge that named none would fall back to
            // whichever has a null id — which is a silent no-render the day
            // one of these ids is renamed.
            sourceHandle: sourceHandle ?? 'out',
            targetHandle: targetHandle ?? 'in',
            label,
            animated: false,
            labelShowBg: false,
            labelStyle: {
              fill: STATUS_COLOR[status],
              fontSize: 9,
              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
              opacity: faded ? 0.5 : 0.9,
            },
            style: {
              stroke: STATUS_COLOR[status],
              strokeWidth: 1.5,
              strokeDasharray: dashed ? '4 3' : undefined,
              opacity: faded ? 0.35 : 0.75,
            },
          }
        },
      ),
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
        <LegendSwatch status="error" />
        {/* The last two apply only to `web_search`, the one conditional stage.
            They are in the shared legend anyway rather than annotated on the
            node, because a reader meeting a dimmed box needs the key in the
            same place as the other three. */}
        <LegendSwatch status="skipped" />
        <LegendSwatch status="pending" />
        <p className="text-severity-muted">
          Click a stage to inspect what it recorded.
        </p>
      </div>

      {/* 460px rather than the original 420px. Adding `web_search` above
          `error_analysis` grew the diagram from 1138x334 to 1138x434, which at
          420px made `fitView` height-bound and shrank every box. 460px puts it
          back to width-bound, so the extra stage costs no legibility. */}
      <div className="h-[460px] overflow-hidden rounded-lg border border-obsidian-800 bg-obsidian-950">
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
          state and is never persisted. Status answers one question — did the
          stage run? — so a stage that executed reads{' '}
          <span className="text-severity-success">Completed</span> even when its
          data was imperfect. Findings such as unparsed lines, entries with no
          timestamp or a model that named no cause are not statuses; click a
          stage to read them.{' '}
          <code className="font-mono">web_search</code> is the exception — it
          owns no report section, so it is judged on its notes alone. That
          supports a weaker claim than the rest, which is why it alone can read{' '}
          <span className="text-severity-muted">Skipped</span> (the run kept
          notes and none are its own, so the detour was not taken) or{' '}
          <span className="text-severity-info">Undetermined</span> (the report
          kept no notes at all, so there is nothing to read the absence of).
        </p>
      )}
    </div>
  )
}
