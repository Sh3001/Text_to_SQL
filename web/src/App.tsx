import { useState } from "react";
import { approvePlan, askQuestion, rejectPlan } from "./api";
import { QueryCard } from "./components/QueryCard";
import type { Turn } from "./types";
import "./App.css";

function progressLabel(kind: string, data: any): string | null {
  switch (kind) {
    case "generating":
      return data.attempt > 0 ? `Thinking (repair attempt ${data.attempt})…` : "Thinking…";
    case "guard_result":
      return data.ok ? "Safety check passed" : "Safety check failed — retrying";
    case "budget_result":
      return data.ok ? "Cost check passed" : "Too expensive — retrying";
    case "repairing":
      return "Fixing the previous attempt…";
    case "executing":
      return "Running the query…";
    case "result":
      return "Got results";
    case "diagnosis":
      return "Diagnosing the empty result…";
    default:
      return null;
  }
}

function newTurn(id: string, question: string): Turn {
  return {
    id,
    question,
    status: "streaming",
    progress: [],
    livePlan: null,
    awaitingApproval: null,
    outcome: null,
    editedSql: "",
    errorMessage: null,
    discarded: false,
  };
}

let turnCounter = 0;

export default function App() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);

  function patchTurn(id: string, patch: Partial<Turn>) {
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  }

  function pushProgress(id: string, kind: string, data: any) {
    const label = progressLabel(kind, data);
    if (!label) return;
    setTurns((prev) =>
      prev.map((t) => (t.id === id ? { ...t, progress: [...t.progress, { kind, label }] } : t))
    );
  }

  async function runStream(id: string, stream: AsyncGenerator<{ event: string; data: any }>) {
    try {
      for await (const { event, data } of stream) {
        if (event === "plan") {
          patchTurn(id, { livePlan: data });
        } else if (event === "awaiting_approval") {
          patchTurn(id, {
            status: "awaiting_approval",
            awaitingApproval: data,
            editedSql: data.safe_sql ?? "",
          });
        } else if (event === "done") {
          patchTurn(id, { status: "done", outcome: data });
        } else if (event === "error") {
          patchTurn(id, { status: "error", errorMessage: data.message });
        } else {
          pushProgress(id, event, data);
        }
      }
    } catch (err) {
      patchTurn(id, { status: "error", errorMessage: err instanceof Error ? err.message : String(err) });
    }
  }

  async function handleAsk(question: string) {
    const id = `t${++turnCounter}`;
    setTurns((prev) => [...prev, newTurn(id, question)]);
    setBusy(true);
    await runStream(id, askQuestion(question));
    setBusy(false);
  }

  async function handleApprove(turnId: string, planId: string, sql: string) {
    patchTurn(turnId, { status: "streaming", progress: [] });
    setBusy(true);
    await runStream(turnId, approvePlan(planId, sql));
    setBusy(false);
  }

  async function handleReject(turnId: string, planId: string) {
    await rejectPlan(planId);
    patchTurn(turnId, { status: "done", discarded: true, awaitingApproval: null });
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    void handleAsk(q);
  }

  return (
    <div className="app">
      <header className="app-header">
        <h1>Query Warden</h1>
        <p className="app-subtitle">Ask a question about the marketplace warehouse.</p>
      </header>

      <main className="chat-thread">
        {turns.length === 0 && (
          <p className="empty-state">
            Try: "How many orders were shipped?" or "What's our net revenue by region?"
          </p>
        )}
        {turns.map((turn) => (
          <div key={turn.id} className="turn">
            <div className="user-question">{turn.question}</div>
            <QueryCard
              turn={turn}
              onApprove={(planId, sql) => handleApprove(turn.id, planId, sql)}
              onReject={(planId) => handleReject(turn.id, planId)}
              onEditSql={(sql) => patchTurn(turn.id, { editedSql: sql })}
            />
          </div>
        ))}
      </main>

      <form className="chat-input-form" onSubmit={handleSubmit}>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Ask a question about the warehouse…"
          disabled={busy}
        />
        <button type="submit" disabled={busy || !input.trim()}>
          Ask
        </button>
      </form>
    </div>
  );
}
