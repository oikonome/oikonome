// Single-series area/line chart. Two variants:
//   "interactive" (default) — 8%-padded y range,
//     4 gridlines, bottom-anchored area, no dots, estimated segment dashed
//     over a faint band, hover crosshair.
//   "static" — the server-rendered charts.line look: zero-anchored area,
//     min/mid/max/0 gridlines, dots when few points, neg_fill zero split.
// Pure SVG, no chart library. Windowing is the caller's job via the
// site-wide RangePicker (3m…All); there is deliberately no wheel-zoom or
// drag-pan, so the product has one windowing vocabulary.
import { useMemo, useRef, useState } from "react";
import { money } from "../api/client";

const PRIMARY = "var(--blue)";
const RED = "var(--red)";

// round the MAGNITUDE and re-apply the sign — Math.round on the
// signed value rounds negative halves toward zero's other side (-1500/1e3
// → -1, not -2) and would print the minus inside ("$-1k")
const shortMoney = (v: number) => {
  const sign = v < 0 ? "-" : "";
  const a = Math.abs(v);
  if (a >= 1e6) return sign + "$" + (a / 1e6).toFixed(2).replace(/\.?0+$/, "") + "M";
  if (a >= 1e3) return sign + "$" + Math.round(a / 1e3).toLocaleString() + "k";
  return sign + "$" + Math.round(a).toLocaleString();
};
// '2022-03' → '03/22' · '2022-03-14' → '03/14/22' · anything else as-is
const pretty = (s: string) => {
  if (s.length === 7 && s[4] === "-") return s.slice(5, 7) + "/" + s.slice(2, 4);
  if (s.length >= 10) return s.slice(5, 7) + "/" + s.slice(8, 10) + "/" + s.slice(2, 4);
  return s;
};

export default function AreaChart({ points, height = 240,
                                    estimatedUntil, estimatedFrom,
                                    xticks, negFill, variant = "interactive",
                                    color = PRIMARY,
                                    second, third, labels,
                                    zeroLine, markX, markLabel,
                                    events = [] }: {
  points: [string, number][];
  height?: number;
  estimatedUntil?: number;      // index up to which the line is an estimate
  estimatedFrom?: number;       // index from which the line is a FORECAST
                                // (dashed tail — cash graph)
  xticks?: "years";
  negFill?: boolean;            // static: split fill/line red below zero
  variant?: "interactive" | "static";
  color?: string;
  second?: [string, number][];  // dashed 6 4 secondary series (orange)
  third?: [string, number][];   // dotted 2 3 tertiary series (green)
  labels?: string[];            // top-right legend when second/third present
  zeroLine?: boolean;           // red dashed "$0 · out of money" threshold
  markX?: number | null;        // critical-date marker index
  markLabel?: string;
  /** dated events on the x-axis — amber lines behind the series, drawn in
   *  the same index space as markX (the red critical marker keeps the
   *  foreground: an autopay is scheduled, running out of money is not) */
  events?: { i: number; label: string }[];
}) {
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const W = 720, H = height, padL = 64, padR = 12, padT = 12, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  const uid = useMemo(() => Math.random().toString(36).slice(2, 8), []);
  const interactive = variant === "interactive";

  const n = points.length;
  if (!n) return <div className="mut" style={{ padding: "2rem" }}>No data yet.</div>;
  const ys = [...points.map((p) => p[1]),
              ...(second ?? []).map((p) => p[1]),
              ...(third ?? []).map((p) => p[1])];
  let ymin = Math.min(...ys, 0), ymax = Math.max(...ys, 0);
  if (interactive) {
    const pad = (ymax - ymin) * 0.08 || 1;
    ymin -= pad; ymax += pad;
  }
  const span = ymax - ymin || 1;
  const X = (i: number) => padL + (iw * i) / Math.max(1, n - 1);
  const Y = (v: number) => padT + ih - (ih * (v - ymin)) / span;
  const pts = points.map((p, i) => [X(i), Y(p[1])] as const);
  const poly = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");

  // static neg_fill: gradient split at the zero line — blue above, red below
  const negZero = !interactive && !!negFill && ymin < 0 && ymax > 0;
  const frac = negZero ? Math.max(0, Math.min(1, (Y(0) - padT) / ih)) : 0;
  const areaFill = negZero ? `url(#fg-${uid})` : color;
  const areaOp = negZero ? 1 : 0.12;
  const lineStroke = negZero ? `url(#sg-${uid})` : color;
  const areaY = interactive ? Y(ymin) : Y(0);   // bottom vs zero anchored

  // gridlines: interactive = 4 evenly spaced; static = min/mid/max/0
  const gvs = interactive
    ? [0, 1, 2, 3].map((g) => ymin + (span * g) / 3)
    : Array.from(new Set([ymin, (ymin + ymax) / 2, ymax, 0])).sort((a, b) => a - b);

  // x labels — one per year, or a few evenly spaced
  const xlab: { i: number; text: string; tick: boolean }[] = [];
  if (xticks === "years") {
    const seen = new Set<string>();
    const years: [number, string][] = [];
    points.forEach((p, i) => {
      const yr = String(p[0]).slice(0, 4);
      if (!seen.has(yr)) { seen.add(yr); years.push([i, yr]); }
    });
    const thin = years.length > 16;
    for (const [i, yr] of years) {
      if (thin && parseInt(yr) % 5 !== 0) continue;
      xlab.push({ i, text: yr, tick: true });
    }
  } else {
    const step = Math.max(1, Math.floor(n / 6));
    for (let i = 0; i < n; i += step) xlab.push({ i, text: String(points[i][0]), tick: false });
  }

  // estimated segment split
  const eu = estimatedUntil === undefined ? null
    : Math.max(0, Math.min(estimatedUntil, n - 1));
  const est = eu !== null && eu > 0 ? pts.slice(0, eu + 1) : null;
  const act = est ? pts.slice(eu!) : null;
  // forecast tail split (mirror of estimatedUntil, at the other end)
  const ef = estimatedFrom === undefined ? null
    : Math.max(0, Math.min(estimatedFrom, n - 1));
  const head = ef !== null && ef < n - 1 ? pts.slice(0, ef + 1) : null;
  const tail = head ? pts.slice(ef!) : null;

  const onMove = (e: React.MouseEvent) => {
    const rect = ref.current!.getBoundingClientRect();
    const fx = ((e.clientX - rect.left) / rect.width) * W;
    const i = Math.round(((fx - padL) / iw) * (n - 1));
    setHover(Math.max(0, Math.min(n - 1, i)));
  };

  const tip = hover !== null ? `${pretty(String(points[hover][0]))}   ${money(points[hover][1])}` : "";
  const bw = Math.max(60, tip.length * 6.2 + 12);
  const bx = hover !== null ? Math.min(Math.max(X(hover) - bw / 2, padL), W - padR - bw) : 0;

  return (
    <svg ref={ref} viewBox={`0 0 ${W} ${H}`}
         style={{ width: "100%", display: "block" }}
         onMouseMove={onMove} onMouseLeave={() => setHover(null)}>
      {negZero && (
        <defs>
          <linearGradient id={`fg-${uid}`} gradientUnits="userSpaceOnUse"
                          x1="0" y1={padT} x2="0" y2={padT + ih}>
            <stop offset="0" stopColor={color} stopOpacity="0.16" />
            <stop offset={frac} stopColor={color} stopOpacity="0.16" />
            <stop offset={frac} stopColor={RED} stopOpacity="0.16" />
            <stop offset="1" stopColor={RED} stopOpacity="0.16" />
          </linearGradient>
          <linearGradient id={`sg-${uid}`} gradientUnits="userSpaceOnUse"
                          x1="0" y1={padT} x2="0" y2={padT + ih}>
            <stop offset="0" stopColor={color} />
            <stop offset={frac} stopColor={color} />
            <stop offset={frac} stopColor={RED} />
            <stop offset="1" stopColor={RED} />
          </linearGradient>
        </defs>
      )}
      <polygon points={`${X(0).toFixed(1)},${areaY.toFixed(1)} ${poly} ${X(n - 1).toFixed(1)},${areaY.toFixed(1)}`}
               fill={areaFill} fillOpacity={areaOp} />
      {gvs.map((gv, i) => (
        <g key={i}>
          <line x1={padL} y1={Y(gv)} x2={W - padR} y2={Y(gv)} className="cgrid" />
          <text x={padL - 6} y={Y(gv) + 3} textAnchor="end" className="ax"
                fill="var(--mut)">{shortMoney(gv)}</text>
        </g>
      ))}
      {/* dated events (card autopays): behind the series, so the money line
          stays the thing you read. The label runs UP the line from the
          baseline — rotated because several can fall in one week and
          horizontal captions would stack into a smear, and bottom-anchored
          because the legend owns the top-right corner (top-anchored labels
          would strike straight through it). */}
      {events.map((e, k) => (
        <g key={`ev${k}`}>
          <line x1={X(e.i)} y1={padT} x2={X(e.i)} y2={padT + ih}
                stroke="var(--mut)" strokeWidth="1" strokeDasharray="3 3"
                strokeOpacity="0.75" />
          <text x={X(e.i) - 3} y={padT + ih - 4} fill="var(--mut)" fontSize="9"
                textAnchor="start"
                transform={`rotate(-90 ${X(e.i) - 3} ${padT + ih - 4})`}>
            {e.label}</text>
        </g>
      ))}
      {est ? (
        <>
          <rect x={X(0)} y={padT} width={X(eu!) - X(0)} height={ih}
                fill={color} fillOpacity="0.05" />
          <polyline points={est.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}
                    fill="none" stroke={color} strokeWidth="2.5"
                    strokeOpacity="0.6" strokeDasharray="5 4" />
          {act!.length > 1 && (
            <polyline points={act!.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}
                      fill="none" stroke={color} strokeWidth="2.5" />
          )}
        </>
      ) : head ? (
        <>
          <rect x={X(ef!)} y={padT} width={X(n - 1) - X(ef!)} height={ih}
                fill={color} fillOpacity="0.05" />
          {head.length > 1 && (
            <polyline points={head.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}
                      fill="none" stroke={lineStroke} strokeWidth="2.5" />
          )}
          <polyline points={tail!.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ")}
                    fill="none" stroke={color} strokeWidth="2.5"
                    strokeOpacity="0.6" strokeDasharray="5 4" />
        </>
      ) : (
        <polyline points={poly} fill="none" stroke={lineStroke} strokeWidth="2.5" />
      )}
      {second && (
        <polyline fill="none" stroke="var(--amber)" strokeWidth="2" strokeDasharray="6 4"
          points={second.slice(0, n).map((p, i) => `${X(i).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ")} />
      )}
      {third && (
        <polyline fill="none" stroke="var(--green)" strokeWidth="2" strokeDasharray="2 3"
          points={third.slice(0, n).map((p, i) => `${X(i).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ")} />
      )}
      {(second || third) && labels && (() => {
        const lx0 = W - padR - 205;
        const entries: [string, string, string | undefined, string][] =
          [[color, "2.5", undefined, labels[0]]];
        if (second && labels.length > 1) entries.push(["var(--amber)", "2", "6 4", labels[1]]);
        if (third && labels.length > 2) entries.push(["var(--green)", "2", "2 3", labels[2]]);
        return entries.map(([col, sw2, dash, lab], i) => (
          <g key={i}>
            <line x1={lx0} y1={padT + 8 + i * 14} x2={lx0 + 18} y2={padT + 8 + i * 14}
                  stroke={col} strokeWidth={sw2} strokeDasharray={dash} />
            <text x={lx0 + 23} y={padT + 11 + i * 14} fontSize="10" className="ax"
                  fill="var(--mut)">{lab}</text>
          </g>
        ));
      })()}
      {zeroLine && (
        <g>
          <line x1={padL} y1={Y(0)} x2={W - padR} y2={Y(0)} stroke={RED}
                strokeWidth="1.5" strokeDasharray="5 3" strokeOpacity="0.85" />
          <text x={W - padR} y={Y(0) - 4} textAnchor="end" fill={RED}
                fontSize="10">$0 · out of money</text>
        </g>
      )}
      {markX !== null && markX !== undefined && markX >= 0 && markX < n && (
        <g>
          <line x1={X(markX)} y1={padT} x2={X(markX)} y2={padT + ih}
                stroke={RED} strokeWidth="1.5" strokeDasharray="5 3" />
          <circle cx={X(markX)} cy={Y(points[markX][1])} r="4.5" fill={RED} />
          {markLabel && (
            <text x={X(markX) + (markX < n / 2 ? 6 : -6)} y={padT + 11}
                  textAnchor={markX < n / 2 ? "start" : "end"} fill={RED}
                  fontSize="11" fontWeight="600">{markLabel}</text>
          )}
        </g>
      )}
      {!interactive && n <= 40 && pts.map(([x, y], i) => (
        <circle key={i} cx={x} cy={y} r="2.5"
                fill={negZero && points[i][1] < 0 ? RED : color} />
      ))}
      {xlab.map(({ i, text, tick }) => (
        <g key={`${i}-${text}`}>
          {tick && <line x1={X(i)} y1={padT + ih} x2={X(i)} y2={padT + ih + 4} className="cgrid" />}
          <text x={X(i)} y={H - 8} textAnchor="middle" className="ax"
                fill="var(--mut)">{text}</text>
        </g>
      ))}
      {hover !== null && (
        <g pointerEvents="none">
          <line x1={X(hover)} x2={X(hover)} y1={padT} y2={padT + ih}
                stroke="var(--mut)" strokeOpacity=".6" strokeDasharray="3 3" />
          <circle cx={X(hover)} cy={Y(points[hover][1])} r="3.5" fill={color} />
          <rect x={bx} y={padT} width={bw} height={16} rx="3" fill="var(--ink)" />
          <text x={bx + bw / 2} y={padT + 11.5} fontSize="11" fill="var(--card)"
                textAnchor="middle">{tip}</text>
        </g>
      )}
    </svg>
  );
}
