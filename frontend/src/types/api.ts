/**
 * The TypeScript view of the LogSherlock HTTP API.
 *
 * These shapes are the backend's ground truth — `backend/schemas.py` for the
 * envelopes and `graph_library/models/` for everything inside
 * `structured_report`. Nothing here is aspirational; a field that is optional
 * below is optional because the backend can omit it, and a field that is
 * nullable is nullable because the column is.
 *
 * Every endpoint lives under `/api`. Where that resolves to is configuration
 * rather than a constant: `VITE_API_BASE_URL` in the repository-root `.env`
 * names the backend origin, and a blank value falls back to a same-origin
 * `/api` handled by the Vite dev proxy (see `services/api.ts` and
 * `vite.config.ts`).
 *
 *     GET    /api/health              -> HealthResponse
 *     POST   /api/investigate         -> InvestigateResponse
 *     POST   /api/investigations      -> PaginatedInvestigationsResponse
 *     POST   /api/investigations/{id} -> InvestigationDetailResponse
 *     DELETE /api/investigations/{id} -> { status: 'deleted', investigation_id }
 *
 * Every failure, at every status code, is `{ "detail": ... }` — a string for a
 * deliberate failure and Pydantic's structured issue list for a 422. See
 * `services/api.ts`, which is the one place that unwraps it.
 */

/**
 * Reasoning tier the graph should spend on one investigation. Selects a model
 * *tier* within the chosen provider; it never changes the deterministic passes.
 */
export type AnalysisMode = 'fast' | 'standard' | 'deep'

/**
 * Vendor the graph's three LLM nodes should reason with. `local` targets any
 * OpenAI-compatible server — vLLM, Ollama, LM Studio, or the project's own mock
 * in `tests/mock_local_llm.py`.
 */
export type LLMProvider = 'openai' | 'anthropic' | 'gemini' | 'deepseek' | 'local'

/**
 * The body of `POST /api/investigate`.
 *
 * The backend sets `extra="forbid"`, so an unknown key is a 422 naming it
 * rather than a silently ignored field.
 */
export interface InvestigateRequest {
  /** The log text itself, not a path to it. Required and non-blank. */
  raw_logs: string
  /** Required and non-blank — it is what a stored investigation is listed by. */
  application_name: string
  /** Defaults to `standard` server-side. */
  analysis_mode?: AnalysisMode
  /** Defaults to `openai` server-side. */
  llm_provider?: LLMProvider
  /** Opt in to the error-analysis web-search detour. Off by default. */
  enable_web_search?: boolean
  /**
   * The key this run is stored under. Optional; ≤ 255 characters.
   *
   * Omit it and the backend mints `inv-graph-<4 hex chars>`, reporting the
   * result in the response. Supply one and it is never replaced — which is what
   * makes a re-run *correct* the stored row instead of adding a second one,
   * because the graph's write is an upsert keyed on this value.
   *
   * That same upsert is why a supplied id is the durable choice: the generated
   * form has a 65,536-value keyspace, so at a few hundred stored records a
   * collision becomes likely, and a collision overwrites the earlier
   * investigation rather than failing.
   */
  investigation_id?: string
}

/**
 * The answer to `POST /api/investigate`.
 *
 * `db_persisted: false` still arrives as a 200: every node degrades rather than
 * raises, so a run whose storage failed is a completed analysis carrying bad
 * news. `investigation_notes` is where the reason lives, and it is the only
 * place a degraded LLM pass or an unreachable database is visible.
 */
export interface InvestigateResponse {
  investigation_id: string
  db_persisted: boolean
  investigation_notes: string[]
}

/**
 * The seven moments the timeline node marks, in narrative order.
 *
 * `logs_start` and `logs_end` are always present. The five error-shaped kinds
 * form one narrative and are emitted only when the payload contained
 * error-level entries at all, so an absent milestone means "not observed" and
 * never "assumed zero". All seven were observed across the stored reports.
 */
export type MilestoneKind =
  | 'logs_start'
  | 'first_error'
  | 'error_onset'
  | 'peak_error_volume'
  | 'recovery_onset'
  | 'last_error'
  | 'logs_end'

/**
 * One entry on an investigation's timeline.
 *
 * Two flavours share one shape, discriminated by `event_type`: a `"bucket"` is
 * an aggregated time window and populates `end_timestamp`, a `"milestone"` is a
 * single notable moment and populates `milestone_kind`. Timestamps are ISO-8601
 * strings. Empty buckets are dropped from the series, so the array is ordered
 * but not contiguous.
 *
 * The inapplicable field of each flavour is `null` rather than absent — the
 * timeline node emits every key on every event so consumers see one stable
 * shape — which is why the two are typed nullable and not merely optional.
 * Verified against stored reports: a bucket carries `milestone_kind: null`, a
 * milestone carries `end_timestamp: null`.
 */
export interface TimelineEvent {
  event_type: 'bucket' | 'milestone' | (string & {})
  timestamp?: string
  end_timestamp?: string | null
  /** Widened past `MilestoneKind` so an added kind renders instead of breaking. */
  milestone_kind?: MilestoneKind | (string & {}) | null
  total_logs?: number
  error_count?: number
  warning_count?: number
  top_loggers?: string[]
  sample_messages?: string[]
  summary?: string
}

/**
 * One row of a statistics distribution.
 *
 * A list of `{value, count}` rows rather than a `{value: count}` mapping,
 * because the ordering is part of the payload and because `value` is genuinely
 * not always a string: `logger_distribution` carries `null` for records that
 * had no logger, and `metadata_distributions` carries integers for keys like
 * `statusCode` and `nights`. Both were observed in the stored reports, which is
 * why this is `unknown` and every renderer has to say what it does with a
 * non-string.
 */
export interface CategoryCount {
  value: unknown
  count: number
}

/** Error and warning shares of the whole dataset, records with no level included. */
export interface SeveritySummary {
  error_count: number
  warning_count: number
  error_ratio: number
  warning_ratio: number
}

/** How much of the payload could be placed on a time axis. */
export interface TimestampCoverage {
  with_timestamp: number
  without_timestamp: number
  /** ISO-8601, or `null` when nothing in the payload carried a timestamp. */
  earliest: string | null
  latest: string | null
}

/**
 * What the parsed dataset contains — arithmetic only, from the statistics node.
 *
 * `metadata_distributions` is keyed by *dynamically discovered* metadata keys,
 * so the field names differ per log ecosystem and cannot be enumerated here.
 */
export interface Statistics {
  level_distribution: CategoryCount[]
  logger_distribution: CategoryCount[]
  severity: SeveritySummary
  timestamp_coverage: TimestampCoverage
  metadata_distributions: Record<string, CategoryCount[]>
}

/**
 * The health of one parsing run.
 *
 * The invariant `total_lines === blank_lines + parsed_lines + malformed_lines`
 * holds, and the two ratio penalties behind `confidence_score` are computed
 * from these counts.
 */
export interface ParserMetrics {
  parser_name: string
  /** Detection confidence on the sampled input, 0.0-1.0. */
  parser_confidence: number
  detected_format: string
  total_lines: number
  blank_lines: number
  parsed_lines: number
  malformed_lines: number
  missing_timestamp_lines: number
}

/**
 * One collapsed failure: a masked template plus everything counted about it.
 *
 * The first six fields are deterministic and authoritative. `explanation` and
 * `is_root_cause_candidate` are the only two the model contributes, and they
 * keep their empty defaults (`""` / `false`) when the LLM pass degraded — so an
 * empty explanation means "not reasoned about", not "nothing to say".
 */
export interface ErrorSignature {
  /** `ERR_001` is always the highest-count signature. */
  signature_id: string
  /** The masked text: variable tokens replaced with `<IP>`, `<NUM>`, `<UUID>`… */
  template: string
  /** Upper-case, as the source wrote it — `ERROR`, `WARN`, `CRITICAL`. */
  severity: string
  count: number
  /** ISO-8601, or a `"line 81"` fallback when the group carried no timestamp. */
  first_seen: string | null
  last_seen: string | null
  /** Possibly empty — a plain-text traceback often yields no logger at all. */
  loggers: string[]
  /** Up to 2 *unmasked* messages, so real values sit beside the template. */
  sample_messages: string[]
  is_root_cause_candidate: boolean
  explanation: string
}

/**
 * The error-analysis node's conclusions.
 *
 * `primary_error_signature_id` is `null` when the model named no root cause, and
 * that is a real and common answer — it is also the single largest penalty
 * against the confidence score, so it must never be rendered as if a cause had
 * been found.
 */
export interface ErrorSummary {
  total_errors_analyzed: number
  unique_signatures_found: number
  primary_error_signature_id: string | null
  signatures: ErrorSignature[]
  cascading_impact_summary: string
}

/** One behaviour the pattern-analysis node reported as abnormal. */
export interface AnomalyItem {
  /** A closed vocabulary, so a consumer can filter and count these. */
  category:
    | 'volume_spike'
    | 'logger_cascade'
    | 'metadata_clustering'
    | 'baseline_shift'
  /** Lower-case, three tiers — not a numeric score. */
  severity: 'info' | 'warning' | 'critical'
  description: string
  affected_loggers?: string[]
  /**
   * An ISO-8601 instant or a readable window such as
   * `"2026-07-29T10:59:20+00:00 to 2026-07-29T10:59:50+00:00"`. `null` when the
   * anomaly is a property of the whole dataset rather than of a moment in it.
   */
  time_window?: string | null
}

/**
 * The pattern-analysis node's conclusions.
 *
 * `anomalies` is empty in most stored reports and that is the expected answer
 * for a payload that behaved normally — an empty list is "nothing abnormal",
 * never "not analysed". `behavioral_synthesis` opening with
 * `"Deterministic summary"` is the one tell that the node fell back to
 * arithmetic instead of reaching a model.
 */
export interface PatternSummary {
  anomalies: AnomalyItem[]
  cross_logger_correlations: string[]
  metadata_insights: string[]
  behavioral_synthesis: string
}

/**
 * The complete stored analysis, partitioned by *provenance* rather than topic.
 *
 * That split is the whole design and should survive into the UI: a reader must
 * be able to tell which numbers are measurements and which sentences are
 * inferences without knowing which node produced what.
 *
 * The index signatures are deliberate, and they are kept even though every
 * field below has now been verified against the stored JSONB. The report is
 * served back verbatim and is deliberately *not* re-validated on the way out,
 * so a document written by an older release must still be readable; a closed
 * shape here would make this file the thing that rejects it.
 *
 * Every field is optional for the same reason. The graph guarantees each
 * section is written, but a renderer that assumed so would lose a whole
 * investigation to one absent key, and the sections a UI can least afford to
 * lose are the deterministic ones that are still exact when the LLM passes
 * degraded.
 *
 * Verified against `inv-graph-001`, `inv-graph-002`, `inv-graph-003` and
 * `inv-graph-abfb` in the live `investigations` table: `metadata` carries
 * exactly these six keys and no `investigation_id` — the id lives on the row
 * and in `InvestigationDetailResponse`, not inside the document.
 */
export interface StructuredInvestigationReport {
  /** Run identity and ingestion health. */
  metadata: {
    application_name?: string
    /**
     * ISO-8601 with offset, from the API that started the run. `""` on a
     * record created by a direct graph invocation, because no node in the
     * graph invents a clock reading — observed on `inv-graph-001`.
     */
    investigation_timestamp?: string
    /** The *normalized* value — `anthropic` where the caller typed `Claude`. */
    analysis_mode?: string
    llm_provider?: string
    /**
     * 0-100 integer, deterministic. `null` means "not measured" and `0` means
     * "measured as zero"; the two must not be collapsed.
     */
    confidence_score?: number | null
    parser_metrics?: ParserMetrics
    [key: string]: any
  }
  /** The prepare-output node's own conclusions. */
  synthesis: {
    root_cause?: string
    executive_summary?: string
    /**
     * A snapshot of the *upstream* notes as they stood when the report was
     * built. Free text from up to eight nodes, and the only place a degraded
     * LLM pass or an unreachable database is visible.
     */
    investigation_notes?: string[]
    [key: string]: any
  }
  /** Arithmetic — reproducible from the same logs. */
  deterministic_outputs: {
    statistics?: Statistics
    timeline?: TimelineEvent[]
    [key: string]: any
  }
  /** The two upstream LLM nodes' conclusions. */
  ai_insights: {
    error_summary?: ErrorSummary
    pattern_summary?: PatternSummary
    [key: string]: any
  }
}

/**
 * One row of the record list — metadata only, never the report.
 *
 * Every field but the id is optional because every column but the primary key
 * is nullable: a run that degraded before it could record a provider still has
 * a row, and that row still belongs in the list.
 */
export interface InvestigationItem {
  investigation_id: string
  application_name?: string
  analysis_mode?: string
  /** ISO-8601 with offset, from the database's own clock. */
  created_at: string
  /** `null` means "not measured", not zero. */
  confidence_score?: number | null
  llm_provider?: string
}

/** A page of the record list, plus what it takes to page through the rest. */
export interface PaginatedInvestigationsResponse {
  /** Newest first, by `created_at DESC NULLS LAST, investigation_id ASC`. */
  items: InvestigationItem[]
  /** Rows in the whole table, not on this page. */
  total: number
  page: number
  limit: number
  /** `0` for an empty table rather than `1`, so it can be tested directly. */
  total_pages: number
}

/**
 * The answer to `POST /api/investigations/{id}` — one full investigation.
 *
 * The report comes back exactly as stored: nothing summarized, reshaped or
 * dropped. A row that exists with a `NULL` report yields an empty object rather
 * than a 404, so an empty-report state is distinct from a missing record.
 */
export interface InvestigationDetailResponse {
  investigation_id: string
  structured_report: StructuredInvestigationReport
}

/**
 * The answer to `GET /api/health`.
 *
 * Deliberately the only endpoint that touches nothing — it does not query the
 * database and does not compile the graph, so a `200` here alongside a `503`
 * elsewhere is the fastest way to confirm the database is what is down.
 */
export interface HealthResponse {
  status: string
  message: string
}
