import { streamSSE } from "./sse";

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
