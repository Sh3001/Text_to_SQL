import { authHeaders, clearSession } from "./auth";
import { streamSSE } from "./sse";
import type { AuditEvent, Conversation, StatsSummary, StoredMessage } from "./types";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8001";

/** A 401 anywhere means the token expired mid-session. Clear it and let
 *  App's auth gate show the sign-in screen rather than leaving the user
 *  clicking a UI whose every request will fail. */
async function authedFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const resp = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...authHeaders(), ...(init.headers ?? {}) },
  });
  if (resp.status === 401) {
    clearSession();
    window.dispatchEvent(new CustomEvent("qw:signed-out"));
    throw new Error("Your session expired. Please sign in again.");
  }
  return resp;
}

async function json<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await authedFetch(path, init);
  if (!resp.ok) {
    const body = await resp.text().catch(() => "");
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const parsed = JSON.parse(body);
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      /* keep the status line */
    }
    throw new Error(detail);
  }
  return resp.json();
}

// ---- asking questions ----

export function askQuestion(question: string, conversationId: string | null) {
  return streamSSE(
    `${API_URL}/api/query`,
    { question, conversation_id: conversationId },
    undefined,
    authHeaders()
  );
}

export function approvePlan(planId: string, sql: string | undefined) {
  return streamSSE(
    `${API_URL}/api/query/approve`,
    { plan_id: planId, sql },
    undefined,
    authHeaders()
  );
}

export async function rejectPlan(planId: string): Promise<void> {
  await authedFetch(`/api/query/${planId}/reject`, { method: "POST" });
}

export async function checkHealth(): Promise<{ status: string; schema_fingerprint: string }> {
  const resp = await fetch(`${API_URL}/api/health`);
  if (!resp.ok) throw new Error(`API unreachable (${resp.status})`);
  return resp.json();
}

// ---- conversations ----

export async function fetchConversations(): Promise<Conversation[]> {
  return (await json<{ conversations: Conversation[] }>("/api/conversations")).conversations;
}

export function createConversation(title?: string): Promise<Conversation> {
  return json<Conversation>("/api/conversations", {
    method: "POST",
    body: JSON.stringify({ title: title ?? null }),
  });
}

export async function fetchConversation(id: string): Promise<StoredMessage[]> {
  return (await json<{ messages: StoredMessage[] }>(`/api/conversations/${id}`)).messages;
}

export async function deleteConversation(id: string): Promise<void> {
  await json(`/api/conversations/${id}`, { method: "DELETE" });
}

export async function renameConversation(id: string, title: string): Promise<void> {
  await json(`/api/conversations/${id}`, { method: "PATCH", body: JSON.stringify({ title }) });
}

// ---- observability (operator only) ----

export function fetchStats(hours = 24): Promise<StatsSummary> {
  return json<StatsSummary>(`/api/stats?hours=${hours}`);
}

export async function fetchAuditEvents(limit = 50): Promise<AuditEvent[]> {
  return (await json<{ events: AuditEvent[] }>(`/api/audit?limit=${limit}`)).events;
}
