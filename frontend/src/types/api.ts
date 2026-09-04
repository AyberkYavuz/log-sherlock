/**
 * The TypeScript view of the LogSherlock HTTP API.
 *
 * Every endpoint lives under `/api`, which Vite proxies to the FastAPI server
 * on `127.0.0.1:8010` in development (see `vite.config.ts`).
 *
 *     GET    /api/health                    -> HealthResponse
 *     POST   /api/investigate               -> InvestigateResponse
 *     POST   /api/investigations            -> PaginatedInvestigationsResponse
 *     POST   /api/investigations/{id}       -> InvestigationDetailResponse
 *     DELETE /api/investigations/{id}       -> { status, investigation_id }
 *
 * NOTE: several shapes below are the agreed frontend contract and do not yet
 * match what `backend/schemas.py` and `graph_library/models/` return today —
 * `InvestigateResponse`, `TimelineEvent`, `AnomalyItem`,
 * `StructuredInvestigationReport`, `InvestigationDetailResponse` and the
 * `status` / `execution_time_ms` fields on `InvestigationItem` in particular.
 * They are written as specified rather than as observed; the two sides need
 * to be reconciled before any component reads a live payload.
 */

/** Reasoning tier the graph should spend on one investigation. */
export type AnalysisMode = 'fast' | 'deep'

/** Vendor the graph's three LLM nodes should reason with. */
export type LLMProvider = 'ollama' | 'openai' | 'anthropic' | 'gemini'

/** The body of `POST /api/investigate`. */
export interface InvestigateRequest {
  /** The log text itself, not a path to it. */
  raw_logs: string
  application_name?: string
  analysis_mode?: AnalysisMode
  llm_provider?: LLMProvider
  /** Opt in to the error-analysis web-search detour. Off by default. */
  enable_web_search?: boolean
}

/** The answer to `POST /api/investigate`. */
export interface InvestigateResponse {
  investigation_id: string
  status: string
  message: string
  execution_time_ms: number
}

/** One entry on an investigation's chronological timeline. */
export interface TimelineEvent {
  timestamp?: string
  log_level?: string
  event_description: string
  raw_slice?: string
}

/** One behaviour the analysis flagged as abnormal. */
export interface AnomalyItem {
  log_snippet: string
  severity: 'CRITICAL' | 'WARNING' | 'INFO'
  error_type: string
  explanation: string
}

/** The complete stored analysis for one investigation. */
export interface StructuredInvestigationReport {
  summary: string
  timeline: TimelineEvent[]
  anomalies: AnomalyItem[]
  root_causes: string[]
  recommendations: string[]
  /** 0-100 integer scale. */
  confidence_score: number
}

/** One row of the record list — metadata only, never the report. */
export interface InvestigationItem {
  investigation_id: string
  status: string
  application_name?: string
  analysis_mode?: string
  created_at: string
  execution_time_ms?: number
}

/** A page of the record list, plus what it takes to page through the rest. */
export interface PaginatedInvestigationsResponse {
  items: InvestigationItem[]
  total: number
  page: number
  limit: number
  /** `0` for an empty table rather than `1`, so it can be tested directly. */
  total_pages: number
}

/** The answer to `POST /api/investigations/{id}` — one full investigation. */
export interface InvestigationDetailResponse {
  investigation_id: string
  status: string
  created_at: string
  request_params: InvestigateRequest
  structured_report?: StructuredInvestigationReport
  error_message?: string
  raw_logs?: string
}

/** The answer to `GET /api/health`. */
export interface HealthResponse {
  status: string
  message: string
}
