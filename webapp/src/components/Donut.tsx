// Donut + legend — a 1:1 port of the legacy server-rendered charts.donut()
// (same palette, ring geometry, center total, legend rows). Pure SVG.

// CVD-safe categorical palette (legacy charts.py PALETTE)
export const PALETTE = ["#4f9bd6", "#e8833a", "#3a9188", "#8a5fb0", "#4f9d5b",
                        "#d9a441", "#c65c4e", "#7a8a99", "#6ab0d6", "#b0894a"];

import { pyround } from "../api/client";

const fmt = (v: number) => {
  const n = pyround(v);              // legacy legends use Python round()
  return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString();
};

export default function Donut({ segments, size = 180 }: {
  segments: [string, number][]; size?: number;
}) {
  const segs = segments.filter(([, v]) => v && v > 0);
  const total = segs.reduce((s, [, v]) => s + v, 0) || 1;
  const r = size / 2 - 14, cx = size / 2, cy = size / 2, sw = 26;
  const circ = 2 * Math.PI * r;
  let off = 0;
  return (
    <div className="donut">
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size}>
        {segs.map(([label, val], i) => {
          const dash = (val / total) * circ;
          const el = (
            <circle key={label} cx={cx} cy={cy} r={r} fill="none"
                    stroke={PALETTE[i % PALETTE.length]} strokeWidth={sw}
                    strokeDasharray={`${dash.toFixed(2)} ${(circ - dash).toFixed(2)}`}
                    strokeDashoffset={-off}
                    transform={`rotate(-90 ${cx} ${cy})`} />
          );
          off += dash;
          return el;
        })}
        <text x={cx} y={cy - 2} textAnchor="middle" className="dc-t">{fmt(total)}</text>
        <text x={cx} y={cy + 16} textAnchor="middle" className="dc-s">total</text>
      </svg>
      <div className="legend">
        {segs.map(([label, val], i) => (
          <div className="lg" key={label}>
            <span className="sw" style={{ background: PALETTE[i % PALETTE.length] }} />
            {label} <b>{fmt(val)}</b>{" "}
            <span className="mut">{((100 * val) / total).toFixed(0)}%</span>
          </div>
        ))}
      </div>
    </div>
  );
}
