import type { ExecutionResult } from "../types";

const MAX_DISPLAY_ROWS = 200;

function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return String(value);
}

export function ResultsTable({ execution }: { execution: ExecutionResult }) {
  const shown = execution.rows.slice(0, MAX_DISPLAY_ROWS);
  const truncated = execution.row_count > shown.length;

  return (
    <div className="results-table-wrap">
      <table className="results-table">
        <thead>
          <tr>
            {execution.columns.map((col) => (
              <th key={col}>{col}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j} className={typeof cell === "number" ? "num" : undefined}>
                  {formatCell(cell)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {truncated && (
        <p className="table-note">
          Showing {shown.length} of {execution.row_count} rows.
        </p>
      )}
    </div>
  );
}
