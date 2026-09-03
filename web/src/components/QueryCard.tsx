import { useState } from "react";
import type { Turn } from "../types";
import { ResultsTable } from "./ResultsTable";
import { ResultChart } from "./ResultChart";

const CONFIDENCE_LABEL: Record<string, string> = {
  high: "High confidence",
  medium: "Medium confidence — worth a look before running",
  low: "Low confidence — worth a look before running",
};

const VERDICT_LABEL: Record<string, string> = {
  answered: "Answered",
  ask: "Needs clarification",
  block: "Blocked for safety",
  diagnose: "No matching rows",
  give_up: "Couldn't produce a safe answer",
};

export function QueryCard({
  turn,
  onApprove,
  onReject,
  onEditSql,
}: {
  turn: Turn;
  onApprove: (planId: string, sql: string) => void;
  onReject: (planId: string) => void;
  onEditSql: (sql: string) => void;
}) {
  const [sqlOpen, setSqlOpen] = useState(turn.status === "awaiting_approval");
  const [feedback, setFeedback] = useState<"up" | "down" | null>(null);

  const plan = turn.awaitingApproval?.plan ?? turn.outcome?.plan ?? turn.livePlan;
  const sql = turn.awaitingApproval?.safe_sql ?? turn.outcome?.last_sql ?? turn.livePlan?.sql ?? "";

  return (
    <div className={`query-card verdict-${turn.outcome?.verdict ?? (turn.status === "error" ? "error" : "pending")}`}>
      {plan?.intent && <p className="card-intent">{plan.intent}</p>}

      {plan?.assumptions && plan.assumptions.length > 0 && (
        <ul className="card-assumptions">
          {plan.assumptions.map((a, i) => (
            <li key={i}>{a}</li>
          ))}
        </ul>
      )}

      {sql && (
        <div className="card-sql-block">
          <button className="sql-toggle" onClick={() => setSqlOpen((v) => !v)}>
            {sqlOpen ? "Hide SQL" : "Show SQL"}
            {plan?.confidence && (
              <span className={`confidence-pill confidence-${plan.confidence}`}>
                {CONFIDENCE_LABEL[plan.confidence] ?? plan.confidence}
              </span>
            )}
          </button>
          {sqlOpen && (
            <textarea
              className="sql-editor"
              value={turn.status === "awaiting_approval" ? turn.editedSql : sql}
              readOnly={turn.status !== "awaiting_approval"}
              spellCheck={false}
              rows={Math.min(12, Math.max(3, sql.split("\n").length))}
              onChange={(e) => onEditSql(e.target.value)}
            />
          )}
        </div>
      )}

      {turn.status === "streaming" && (
        <div className="progress-log">
          {turn.progress.map((p, i) => (
            <div key={i} className={`progress-line ${i === turn.progress.length - 1 ? "current" : "past"}`}>
              {p.label}
            </div>
          ))}
        </div>
      )}

      {turn.status === "awaiting_approval" && turn.awaitingApproval && (
        <div className="approval-gate">
          <p className="approval-note">
            This query isn't at high confidence — read the SQL above before running it. Edit it
            directly if something looks wrong; it's re-checked by the safety guard either way.
          </p>
          <div className="approval-buttons">
            <button
              className="approve-btn"
              onClick={() => onApprove(turn.awaitingApproval!.plan_id, turn.editedSql)}
            >
              Approve &amp; run
            </button>
            <button className="reject-btn" onClick={() => onReject(turn.awaitingApproval!.plan_id)}>
              Discard
            </button>
          </div>
        </div>
      )}

      {turn.status === "error" && <p className="error-message">{turn.errorMessage}</p>}

      {turn.discarded && <p className="outcome-message outcome-discarded">Discarded — not run.</p>}

      {turn.outcome && turn.outcome.verdict !== "answered" && (
        <p className={`outcome-message outcome-${turn.outcome.verdict}`}>
          <strong>{VERDICT_LABEL[turn.outcome.verdict]}.</strong> {turn.outcome.message}
        </p>
      )}

      {turn.outcome?.diagnosis && (
        <div className="diagnosis-block">
          <table className="diagnosis-table">
            <thead>
              <tr>
                <th>Filter (applied cumulatively)</th>
                <th>Rows remaining</th>
              </tr>
            </thead>
            <tbody>
              {turn.outcome.diagnosis.checks.map((c, i) => (
                <tr key={i} className={c.predicate_sql === turn.outcome!.diagnosis!.culprit ? "culprit-row" : undefined}>
                  <td>{c.predicate_sql}</td>
                  <td className="num">{c.cumulative_row_count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {turn.outcome?.execution && (
        <>
          <ResultsTable execution={turn.outcome.execution} />
          {plan?.chart && <ResultChart chart={plan.chart} execution={turn.outcome.execution} />}
          <div className="card-footer">
            <span className="result-meta">
              {turn.outcome.execution.row_count} row{turn.outcome.execution.row_count === 1 ? "" : "s"} ·{" "}
              {Math.round(turn.outcome.execution.duration_ms)}ms
              {turn.outcome.repair_attempts_used > 0 &&
                ` · ${turn.outcome.repair_attempts_used} repair attempt${turn.outcome.repair_attempts_used === 1 ? "" : "s"}`}
            </span>
            <span className="feedback-buttons">
              <button
                className={`feedback-btn ${feedback === "up" ? "active" : ""}`}
                aria-label="Good answer"
                onClick={() => setFeedback("up")}
              >
                ✓
              </button>
              <button
                className={`feedback-btn ${feedback === "down" ? "active" : ""}`}
                aria-label="Wrong answer"
                onClick={() => setFeedback("down")}
              >
                ✕
              </button>
            </span>
          </div>
        </>
      )}
    </div>
  );
}
