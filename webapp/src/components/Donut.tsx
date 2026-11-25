// Donut + legend: ring geometry, center total, legend rows. Pure SVG.

// CVD-safe categorical palette, the same one as the server's charts.py
export const PALETTE = ["#4f9bd6", "#e8833a", "#3a9188", "#8a5fb0", "#4f9d5b",
                        "#d9a441", "#c65c4e", "#7a8a99", "#6ab0d6", "#b0894a"];

import type { RingFigures } from "../api/client";

// the shown figures are whole dollars already (reporting.shown_dollars)
const fmt = (n: number) => (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString();

export default function Donut({ segments, figures, size = 180 }: {
  segments: [string, number][];
  // the ring's figures, server-decided: the legend prints `shown` — whole
  // dollars adding up to the net, and the drawn rows' shares adding up to
  // 100 — and computes neither
  figures: RingFigures;
  size?: number;
}) {
  // a ring can only draw what is owned; what is owed (card debt, an
  // institution that nets negative) is listed under it, with the net the
  // page's own total is — so the ring never reads as a second total
  const shown = figures.shown;
  const drawnRows = shown.rows.filter((r) => r.share !== null);
  const owedRows = shown.rows.filter((r) => r.owed);
  // the slices are the rows the legend lists as drawn, in its order:
  // `shown.rows` is `segments` row for row, paired by position, never by
  // label (two institutions can share one, an unnamed connection's
  // "Other" beside one named "Other")
  const segs = segments.filter((_s, i) => shown.rows[i]?.share != null);
  // the ring's geometry divides by what it draws; the figures it PRINTS
  // (the centre total, the legend, the Net line) are the server's
  const drawn = segs.reduce((s, [, v]) => s + v, 0);
  const total = drawn || 1;
  const r = size / 2 - 14, cx = size / 2, cy = size / 2, sw = 26;
  const circ = 2 * Math.PI * r;
  let off = 0;
  return (
    <div className="donut">
      {/* the legend reads every slice; the ring's one figure of its own,
          the centre total, is said in words before it */}
      <span className="sr-only">{owedRows.length ? "Assets" : "Total"} {fmt(shown.owned)}</span>
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} aria-hidden="true">
        {segs.map(([, val], i) => {
          const dash = (val / total) * circ;
          const el = (
            <circle key={i} cx={cx} cy={cy} r={r} fill="none"
                    stroke={PALETTE[i % PALETTE.length]} strokeWidth={sw}
                    strokeDasharray={`${dash.toFixed(2)} ${(circ - dash).toFixed(2)}`}
                    strokeDashoffset={-off}
                    transform={`rotate(-90 ${cx} ${cy})`} />
          );
          off += dash;
          return el;
        })}
        <text x={cx} y={cy - 2} textAnchor="middle" className="dc-t">{fmt(shown.owned)}</text>
        <text x={cx} y={cy + 16} textAnchor="middle" className="dc-s">{owedRows.length ? "assets" : "total"}</text>
      </svg>
      <div className="legend">
        {drawnRows.map((row, i) => (
          <div className="lg" key={i}>
            <span className="sw" style={{ background: PALETTE[i % PALETTE.length] }} />
            {row.label} <b>{fmt(row.dollars)}</b>{" "}
            <span className="mut">{row.share}%</span>
          </div>
        ))}
        {owedRows.map((row, i) => (
          <div className="lg" key={`owed-${i}`}>
            <span className="sw" style={{ background: "transparent",
                                          border: "1px solid var(--line)" }} />
            {row.label} <b className="neg">{fmt(row.dollars)}</b>{" "}
            <span className="mut">owed · not drawn</span>
          </div>
        ))}
        {owedRows.length > 0 && (
          <div className="lg" style={{ borderTop: "1px solid var(--line)",
                                       paddingTop: ".25rem" }}>
            <span className="sw" style={{ background: "transparent" }} />
            Net <b>{fmt(shown.net)}</b>
          </div>
        )}
      </div>
    </div>
  );
}
