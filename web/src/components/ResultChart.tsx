import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ChartSpec, ExecutionResult } from "../types";

// Validates the model's proposed axes against the result's actual
// columns before rendering; falls back to the table if they don't exist.
export function ResultChart({ chart, execution }: { chart: ChartSpec; execution: ExecutionResult }) {
  if (chart.kind === "none") return null;

  const xIdx = execution.columns.indexOf(chart.x);
  const yIdx = execution.columns.indexOf(chart.y);
  if (xIdx === -1 || yIdx === -1) {
    return (
      <p className="chart-fallback-note">
        Chart skipped — the suggested axes ("{chart.x}", "{chart.y}") aren't in this result's
        columns ({execution.columns.join(", ")}). Showing the table only.
      </p>
    );
  }

  const data = execution.rows.map((row) => ({
    [chart.x]: formatAxisLabel(row[xIdx]),
    [chart.y]: Number(row[yIdx]),
  }));

  const hasNonNumericY = data.some((d) => Number.isNaN(d[chart.y]));
  if (hasNonNumericY) {
    return (
      <p className="chart-fallback-note">
        Chart skipped — "{chart.y}" isn't numeric in this result. Showing the table only.
      </p>
    );
  }

  return (
    <div className="chart-wrap">
      <ResponsiveContainer width="100%" height={260}>
        {chart.kind === "bar" ? (
          <BarChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
            <CartesianGrid strokeDasharray="3 3" className="chart-grid" />
            <XAxis dataKey={chart.x} tick={{ fontSize: 12 }} />
            <YAxis tick={{ fontSize: 12 }} tickFormatter={formatYAxisTick} width={64} />
            <Tooltip formatter={(v) => formatYAxisTick(Number(v))} />
            <Bar dataKey={chart.y} className="chart-bar" radius={[3, 3, 0, 0]} />
          </BarChart>
        ) : (
          <LineChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 8 }}>
            <CartesianGrid strokeDasharray="3 3" className="chart-grid" />
            <XAxis dataKey={chart.x} tick={{ fontSize: 12 }} />
            <YAxis tick={{ fontSize: 12 }} tickFormatter={formatYAxisTick} width={64} />
            <Tooltip formatter={(v) => formatYAxisTick(Number(v))} />
            <Line type="monotone" dataKey={chart.y} className="chart-line" strokeWidth={2} dot={{ r: 3 }} />
          </LineChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}

// Recharts' default Y-axis formatting on large values (hundreds of
// millions) overlapped ticks into unreadable repeated zeros.
const compactNumber = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });

function formatYAxisTick(v: number): string {
  return Number.isFinite(v) ? compactNumber.format(v) : String(v);
}

function formatAxisLabel(v: unknown): string {
  if (v === null || v === undefined) return "—";
  const s = String(v);
  // Postgres timestamptz values arrive as Python's str(datetime) — trim
  // to the date for a readable axis tick.
  const isoish = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/;
  return isoish.test(s) ? s.slice(0, 10) : s;
}
