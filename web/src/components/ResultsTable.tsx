import type { ExecutionResult } from "../types";

const MAX_DISPLAY_ROWS = 200;

// Postgres `numeric` arrives as a JSON *string*, not a number: psycopg
// gives Python a Decimal, and the SSE layer serialises it with
// `default=str` to avoid float rounding. So a revenue column shows up as
// "18308535.867826000000000000000000" and `typeof cell === "number"` is
// false. Both facts have to be handled here, or currency columns render
// with 24 decimal places and lose their right-alignment.
const NUMERIC_RE = /^-?\d+(\.\d+)?$/;

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && NUMERIC_RE.test(value.trim())) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

function formatNumber(n: number): string {
  // Integers stay exact (ids, counts, years). Anything fractional is a
  // measured quantity, where two decimals is what a reader wants.
  if (Number.isInteger(n)) return n.toLocaleString();
  return n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  const n = asNumber(value);
  if (n !== null) return formatNumber(n);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

export function ResultsTable({ execution }: { execution: ExecutionResult }) {
  const shown = execution.rows.slice(0, MAX_DISPLAY_ROWS);
  const truncated = execution.row_count > shown.length;

  // Align a column right only if every value in it is numeric, so a
  // mixed column doesn't end up ragged.
  const numericColumn = execution.columns.map((_, j) =>
    shown.length > 0 && shown.every((row) => row[j] === null || asNumber(row[j]) !== null)
  );

  return (
    <div className="results-table-wrap">
      <table className="results-table">
        <thead>
          <tr>
            {execution.columns.map((col, j) => (
              <th key={col} className={numericColumn[j] ? "num" : undefined}>
                {col.replace(/_/g, " ")}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => {
                const exact = asNumber(cell);
                return (
                  <td
                    key={j}
                    className={numericColumn[j] ? "num" : undefined}
                    // Full precision stays available without cluttering the cell.
                    title={exact !== null && !Number.isInteger(exact) ? String(cell) : undefined}
                  >
                    {formatCell(cell)}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
      {truncated && (
        <p className="table-note">
          Showing {shown.length} of {execution.row_count.toLocaleString()} rows.
        </p>
      )}
    </div>
  );
}
