// Mirrors api/app/api/sse.py's serialize_outcome / serialize_plan_result
// and the progress-event payloads answer.py's on_event callback emits —
// kept in sync by hand (no shared schema codegen for a project this
// size); if a field here stops matching, the browser console shows
// `undefined` rather than a type error, so double check server-side
// changes get mirrored here too.

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
