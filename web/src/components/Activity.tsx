import { useEffect, useState } from "react";
import { fetchAuditEvents, fetchStats } from "../api";
import type { AuditEvent, StatsSummary } from "../types";

const VERDICT_ORDER = ["answered", "ask", "diagnose", "block", "give_up"];

export function Activity() {
  const [stats, setStats] = useState<StatsSummary | null>(null);
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const [s, e] = await Promise.all([fetchStats(24), fetchAuditEvents(50)]);
      setStats(s);
      setEvents(e);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const byVerdict = new Map(stats?.by_verdict.map((v) => [v.verdict, v]) ?? []);

  return (
    <div className="activity">
      <div className="activity-header">
        <h2>Last 24 hours</h2>
        <button className="refresh-btn" onClick={() => void load()} disabled={loading}>
          {loading ? "Loading…" : "Refresh"}
        </button>
      </div>

      {error && <p className="error-message">{error}</p>}

      {stats && (
        <>
          <div className="stat-tiles">
            <div className="stat-tile">
              <span className="stat-value">{stats.total_queries}</span>
              <span className="stat-label">questions asked</span>
            </div>
            <div className="stat-tile">
              <span className="stat-value">
                {stats.avg_duration_ms !== null ? `${Math.round(stats.avg_duration_ms)}ms` : "—"}
              </span>
              <span className="stat-label">avg. latency</span>
            </div>
            <div className="stat-tile">
              <span className="stat-value">{byVerdict.get("block")?.count ?? 0}</span>
              <span className="stat-label">blocked for safety</span>
            </div>
          </div>

          <table className="verdict-table">
            <thead>
              <tr>
                <th>Verdict</th>
                <th>Count</th>
                <th>Avg latency</th>
                <th>Avg repairs</th>
              </tr>
            </thead>
            <tbody>
              {VERDICT_ORDER.filter((v) => byVerdict.has(v)).map((v) => {
                const row = byVerdict.get(v)!;
                return (
                  <tr key={v}>
                    <td>
                      <span className={`verdict-tag verdict-${v}`}>{v}</span>
                    </td>
                    <td className="num">{row.count}</td>
                    <td className="num">{row.avg_duration_ms !== null ? `${Math.round(row.avg_duration_ms)}ms` : "—"}</td>
                    <td className="num">{row.avg_repair_attempts ?? "—"}</td>
                  </tr>
                );
              })}
              {stats.by_verdict.length === 0 && (
                <tr>
                  <td colSpan={4} className="empty-row">
                    No questions asked yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </>
      )}

      <h2 className="audit-heading">Blocked &amp; given-up attempts</h2>
      <p className="audit-subtitle">
        Every safety-critical event, whether or not a question is currently in the thread above.
      </p>
      {events.length === 0 ? (
        <p className="empty-row">None in the last 24 hours.</p>
      ) : (
        <div className="audit-list">
          {events.map((e) => (
            <div key={e.request_id} className={`audit-row verdict-${e.verdict}`}>
              <div className="audit-row-top">
                <span className={`verdict-tag verdict-${e.verdict}`}>{e.verdict}</span>
                <span className="audit-time">{new Date(e.occurred_at).toLocaleString()}</span>
                {e.edited && <span className="edited-tag">edited</span>}
              </div>
              <p className="audit-question">{e.question}</p>
              <p className="audit-message">{e.message}</p>
              {e.generated_sql && (
                <pre className="audit-sql">{e.generated_sql}</pre>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
