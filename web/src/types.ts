// Mirrors api/app/api/sse.py's serialize_outcome / serialize_plan_result
// — kept in sync by hand, no shared schema codegen.

export type ChartSpec = {
  kind: "bar" | "line" | "none";
  x: string;
  y: string;
};

export type Confidence = "high" | "medium" | "low";

export type SqlPlan = {
  intent: string;
  assumptions: string[];
  tables_used: string[];
  sql: string;
  chart: ChartSpec | null;
  confidence: Confidence;
  clarifying_question: string | null;
};

export type ExecutionResult = {
  columns: string[];
  rows: unknown[][];
  row_count: number;
  duration_ms: number;
};

export type PredicateCheck = {
  predicate_sql: string;
  cumulative_row_count: number;
};

export type ZeroRowDiagnosis = {
  baseline_row_count: number;
  checks: PredicateCheck[];
  culprit: string | null;
  message: string;
};

export type Verdict = "answered" | "ask" | "block" | "diagnose" | "give_up";

export type AnswerOutcome = {
  verdict: Verdict;
  question: string;
  plan: SqlPlan | null;
  execution: ExecutionResult | null;
  diagnosis: ZeroRowDiagnosis | null;
  message: string;
  repair_attempts_used: number;
  last_sql: string | null;
  failure_kind: string | null;
};

export type AwaitingApproval = {
  plan_id: string;
  ready: boolean;
  question: string;
  plan: SqlPlan | null;
  safe_sql: string | null;
  repair_attempts_used: number;
  terminal_outcome: AnswerOutcome | null;
};

// One chat turn's live state as SSE events arrive — a superset of
// server payloads plus the client-only bookkeeping (status, log,
// editedSql for the approval-gate textarea).
export type TurnStatus = "streaming" | "awaiting_approval" | "done" | "error";

export type ProgressEntry = {
  kind: string;
  label: string;
};

export type Turn = {
  id: string;
  question: string;
  status: TurnStatus;
  progress: ProgressEntry[];
  livePlan: Partial<SqlPlan> | null;
  awaitingApproval: AwaitingApproval | null;
  outcome: AnswerOutcome | null;
  editedSql: string;
  errorMessage: string | null;
  discarded: boolean;
};

// ---- observability ----

export type VerdictStat = {
  verdict: string;
  count: number;
  avg_duration_ms: number | null;
  avg_repair_attempts: number | null;
};

export type StatsSummary = {
  window_hours: number;
  total_queries: number;
  avg_duration_ms: number | null;
  by_verdict: VerdictStat[];
};

export type AuditEvent = {
  request_id: string;
  occurred_at: string;
  tenant_id: number;
  question: string;
  verdict: string;
  failure_kind: string | null;
  generated_sql: string | null;
  safe_sql: string | null;
  edited: boolean;
  repair_attempts: number;
  message: string;
};


// ---- history ----

export type Conversation = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count: number;
};

export type StoredMessage = {
  id: number;
  role: "user" | "assistant";
  content: string;
  outcome: AnswerOutcome | null;
  created_at: string;
};


