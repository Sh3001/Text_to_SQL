import { streamSSE } from "./sse";
import type { AuditEvent, StatsSummary } from "./types";

const API_URL = import.meta.env.VITE_API_URL || "http://127.0.0.1:8001";

export function askQuestion(question: string, tenantId = 1) {
  return streamSSE(`${API_URL}/api/query`, { question, tenant_id: tenantId });
}

export function approvePlan(planId: string, sql: string | undefined, tenantId = 1) {
  return streamSSE(`${API_URL}/api/query/approve`, { plan_id: planId, sql, tenant_id: tenantId });
}

export async function rejectPlan(planId: string): Promise<void> {
  await fetch(`${API_URL}/api/query/${planId}/reject`, { method: "POST" });
}

export async function checkHealth(): Promise<{ status: string; schema_fingerprint: string }> {
  const resp = await fetch(`${API_URL}/api/health`);
  if (!resp.ok) throw new Error(`API unreachable (${resp.status})`);
  return resp.json();
}

export async function fetchStats(hours = 24): Promise<StatsSummary> {
  const resp = await fetch(`${API_URL}/api/stats?hours=${hours}`);
  if (!resp.ok) throw new Error(`stats unreachable (${resp.status})`);
  return resp.json();
}

export async function fetchAuditEvents(limit = 50): Promise<AuditEvent[]> {
  const resp = await fetch(`${API_URL}/api/audit?limit=${limit}`);
  if (!resp.ok) throw new Error(`audit unreachable (${resp.status})`);
  const body = await resp.json();
  return body.events;
}
