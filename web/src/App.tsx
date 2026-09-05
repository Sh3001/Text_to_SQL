import { useCallback, useEffect, useState } from "react";
import {
  approvePlan,
  askQuestion,
  createConversation,
  deleteConversation,
  fetchConversation,
  fetchConversations,
  rejectPlan,
} from "./api";
import { clearSession, fetchMe, getStoredUser } from "./auth";
import { Activity } from "./components/Activity";
import { QueryCard } from "./components/QueryCard";
import { SignIn } from "./components/SignIn";
import { Sidebar } from "./components/Sidebar";
import type { Conversation, Turn, User } from "./types";
import "./App.css";

const SUGGESTIONS = [
  "How many orders were shipped?",
  "What is our net revenue by region?",
  "Top 5 products by revenue last month",
  "What's the refund rate by category?",
];

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
  const [user, setUser] = useState<User | null>(getStoredUser());
  const [checkingSession, setCheckingSession] = useState(true);

  const [turns, setTurns] = useState<Turn[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [view, setView] = useState<"chat" | "activity">("chat");

  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Confirm the stored token is still good before trusting the cached user.
  useEffect(() => {
    fetchMe()
      .then((me) => setUser(me))
      .catch(() => setUser(null))
      .finally(() => setCheckingSession(false));
  }, []);

  // api.ts fires this when any request comes back 401 mid-session.
  useEffect(() => {
    const onSignedOut = () => {
      setUser(null);
      setTurns([]);
      setConversations([]);
      setActiveId(null);
    };
    window.addEventListener("qw:signed-out", onSignedOut);
    return () => window.removeEventListener("qw:signed-out", onSignedOut);
  }, []);

  const refreshConversations = useCallback(async () => {
    try {
      setConversations(await fetchConversations());
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }, []);

  useEffect(() => {
    if (user) void refreshConversations();
  }, [user, refreshConversations]);

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
    // A question asked from a blank thread creates the conversation it
    // belongs to, so nothing is written until there's something to write.
    let conversationId = activeId;
    if (!conversationId) {
      try {
        const convo = await createConversation();
        conversationId = convo.id;
        setActiveId(convo.id);
      } catch (err) {
        setLoadError(err instanceof Error ? err.message : String(err));
        return;
      }
    }

    const id = `t${++turnCounter}`;
    setTurns((prev) => [...prev, newTurn(id, question)]);
    setBusy(true);
    await runStream(id, askQuestion(question, conversationId));
    setBusy(false);
    void refreshConversations();
  }

  async function handleApprove(turnId: string, planId: string, sql: string) {
    patchTurn(turnId, { status: "streaming", progress: [] });
    setBusy(true);
    await runStream(turnId, approvePlan(planId, sql));
    setBusy(false);
    void refreshConversations();
  }

  async function handleReject(turnId: string, planId: string) {
    await rejectPlan(planId);
    patchTurn(turnId, { status: "done", discarded: true, awaitingApproval: null });
  }

  async function handleSelectConversation(id: string) {
    setActiveId(id);
    setView("chat");
    setLoadError(null);
    try {
      const messages = await fetchConversation(id);
      // Stored messages come back as user/assistant pairs; rebuild each
      // pair into the same Turn shape a live answer produces, so
      // QueryCard renders history and live turns identically.
      const restored: Turn[] = [];
      for (let i = 0; i < messages.length; i++) {
        const m = messages[i];
        if (m.role !== "user") continue;
        const reply = messages[i + 1]?.role === "assistant" ? messages[i + 1] : null;
        restored.push({
          ...newTurn(`h${m.id}`, m.content),
          status: "done",
          outcome: reply?.outcome ?? null,
        });
      }
      setTurns(restored);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
      setTurns([]);
    }
  }

  function handleNewConversation() {
    setActiveId(null);
    setTurns([]);
    setView("chat");
    setLoadError(null);
  }

  async function handleDeleteConversation(id: string) {
    try {
      await deleteConversation(id);
      if (id === activeId) handleNewConversation();
      await refreshConversations();
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : String(err));
    }
  }

  function handleSignOut() {
    clearSession();
    setUser(null);
    setTurns([]);
    setConversations([]);
    setActiveId(null);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (!q || busy) return;
    setInput("");
    void handleAsk(q);
  }

  if (checkingSession) {
    return (
      <div className="signin-screen">
        <div className="signin-card">
          <p className="signin-checking">Checking your session…</p>
        </div>
      </div>
    );
  }

  if (!user) return <SignIn onSignedIn={setUser} />;

  return (
    <div className={`shell ${collapsed ? "shell-collapsed" : ""}`}>
      <Sidebar
        conversations={conversations}
        activeId={activeId}
        user={user}
        collapsed={collapsed}
        onSelect={(id) => void handleSelectConversation(id)}
        onNew={handleNewConversation}
        onDelete={(id) => void handleDeleteConversation(id)}
        onSignOut={handleSignOut}
        onToggle={() => setCollapsed((v) => !v)}
      />

      <div className="app">
        <header className="app-header">
          <div className="app-header-top">
            <div>
              <h1>{activeId ? conversations.find((c) => c.id === activeId)?.title ?? "Query Warden" : "Query Warden"}</h1>
              <p className="app-subtitle">Ask a question about the marketplace warehouse.</p>
            </div>
            <nav className="view-tabs">
              <button className={view === "chat" ? "active" : ""} onClick={() => setView("chat")}>
                Chat
              </button>
              {user.role === "operator" && (
                <button
                  className={view === "activity" ? "active" : ""}
                  onClick={() => setView("activity")}
                >
                  Activity
                </button>
              )}
            </nav>
          </div>
        </header>

        {loadError && <p className="banner-error">{loadError}</p>}

        {view === "activity" && user.role === "operator" ? (
          <Activity />
        ) : (
          <>
            <main className="chat-thread">
              {turns.length === 0 && (
                <div className="empty-state">
                  <h2>What would you like to know?</h2>
                  <p>
                    Answers come with the SQL that produced them. Anything the model isn't confident
                    about waits for you to approve it first.
                  </p>
                  <div className="suggestions">
                    {SUGGESTIONS.map((s) => (
                      <button key={s} className="suggestion" onClick={() => void handleAsk(s)} disabled={busy}>
                        {s}
                      </button>
                    ))}
                  </div>
                </div>
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
                {busy ? "Asking…" : "Ask"}
              </button>
            </form>
          </>
        )}
      </div>
    </div>
  );
}
