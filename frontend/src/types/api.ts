/**
 * The TypeScript view of the LogSherlock HTTP API.
 *
 * These shapes are the backend's ground truth — `backend/schemas.py` for the
 * envelopes and `graph_library/models/` for everything inside
 * `structured_report`. Nothing here is aspirational; a field that is optional
 * below is optional because the backend can omit it, and a field that is
 * nullable is nullable because the column is.
 *
 * Every endpoint lives under `/api`, which Vite proxies to the FastAPI server
 * on `127.0.0.1:8010` in development (see `vite.config.ts`).
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
  event_type: string
  timestamp?: string
  end_timestamp?: string | null
  milestone_kind?: string | null
  total_logs?: number
  error_count?: number
  warning_count?: number
  top_loggers?: string[]
  sample_messages?: string[]
  summary?: string
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
 * The complete stored analysis, partitioned by *provenance* rather than topic.
 *
 * That split is the whole design and should survive into the UI: a reader must
 * be able to tell which numbers are measurements and which sentences are
 * inferences without knowing which node produced what.
 *
 * The index signatures are deliberate. Each section carries more than the
 * fields named here — `metadata.parser_metrics`,
 * `deterministic_outputs.statistics`, `ai_insights.error_summary` and the rest
 * — and the report is stored as JSONB and served back verbatim without
 * re-validation, so a document written by an older release must still be
 * readable. Fields are narrowed here as views come to need them.
 */
export interface StructuredInvestigationReport {
  /** Run identity and ingestion health. */
  metadata: {
    /**
     * 0-100 integer, deterministic. `null` means "not measured" and `0` means
     * "measured as zero"; the two must not be collapsed.
     */
    confidence_score?: number | null
    [key: string]: any
  }
  /** The prepare-output node's own conclusions. */
  synthesis: {
    executive_summary?: string
    root_cause?: string
    [key: string]: any
  }
  /** Arithmetic — reproducible from the same logs. */
  deterministic_outputs: {
    timeline?: TimelineEvent[]
    [key: string]: any
  }
  /** The two upstream LLM nodes' conclusions. */
  ai_insights: {
    pattern_summary?: {
      anomalies?: AnomalyItem[]
    }
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
