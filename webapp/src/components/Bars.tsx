// Horizontal ranked bars: barlist/barrow/bartrack markup, red for
// negatives, top-20 cap.

import { pyround } from "../api/client";

const PRIMARY = "var(--blue)";

const fmt = (v: number) => {
  const n = pyround(v);              // bar values use Python's round()
  return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString();
};

export default function Bars({ rows, maxRows = 20 }: {
  rows: [string, number][]; maxRows?: number;
}) {
  const shown = rows.slice(0, maxRows);
  if (!shown.length) return <div className="mut">No data.</div>;
  const mx = Math.max(...shown.map(([, v]) => Math.abs(v)), 1);
  return (
    <div className="barlist">
      {shown.map(([label, val]) => (
        <div className="barrow" key={label}>
          <div className="barlbl">{label}</div>
          <div className="bartrack">
            <div className="barfill"
                 style={{ width: `${((100 * Math.abs(val)) / mx).toFixed(1)}%`,
                          background: val < 0 ? "var(--red)" : PRIMARY }} />
          </div>
          <div className="barval">{fmt(val)}</div>
        </div>
      ))}
    </div>
  );
}
