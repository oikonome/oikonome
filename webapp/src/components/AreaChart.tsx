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
import { useFitWidth } from "./useFitWidth";
import { money } from "../api/client";
import { chartSentence, chartTable, hoverRows, stepPoint, type ChartSeries } from "../chartsummary";
import { ShowNumbers } from "./ChartText";

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
                                    second, third, band, labels,
                                    zeroLine, markX, markLabel,
                                    events = [], subject, tipExtra = [] }: {
  points: [string, number][];
  /** what the line is, for the sentence a screen reader hears and the
   *  "Show numbers" table: "Net worth" */
  subject: string;
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
  /** a shaded band (low and high edge per point, the same days as
   *  `points`), named by labels[1] when no second line takes it */
  band?: { lo: [string, number][]; hi: [string, number][] };
  labels?: string[];            // top-right legend when second/third present
  zeroLine?: boolean;           // red dashed "$0 · out of money" threshold
  markX?: number | null;        // critical-date marker index
  markLabel?: string;
  /** dated events on the x-axis — amber lines behind the series, drawn in
   *  the same index space as markX (the red critical marker keeps the
   *  foreground: an autopay is scheduled, running out of money is not) */
  events?: { i: number; label: string; note?: string }[];
  /** lines the tooltip lists for the hovered day without drawing them
   *  (the cash timeline's other card scenarios), in the order given */
  tipExtra?: ChartSeries[];
}) {
  const ref = useRef<SVGSVGElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  // what the arrow keys last landed on, read out by the live region (the
  // mouse moves the same tooltip without speaking)
  const [spoken, setSpoken] = useState("");
  // The drawing is as wide as the space it is given, down to a phone's
  // card. A fixed 720 scaled to fit made every label a third of its size
  // there — a chart nobody could read the axis of.
  const W = useFitWidth(ref, 720, points.length === 0);
  const H = height, padL = W < 480 ? 46 : 64, padR = 12, padT = 12, padB = 26;
  const iw = W - padL - padR, ih = H - padT - padB;
  const uid = useMemo(() => Math.random().toString(36).slice(2, 8), []);
  const interactive = variant === "interactive";

  const n = points.length;
  if (!n) return <div className="mut" style={{ padding: "2rem" }}>No data yet.</div>;
  const ys = [...points.map((p) => p[1]),
              ...(second ?? []).map((p) => p[1]),
              ...(third ?? []).map((p) => p[1]),
              ...(band?.lo ?? []).map((p) => p[1]),
              ...(band?.hi ?? []).map((p) => p[1])];
  let ymin = Math.min(...ys, 0), ymax = Math.max(...ys, 0);
  let step = 0;
  if (interactive) {
    // A series that never goes below zero keeps its floor AT zero, not at
    // a negative figure no point ever had (and the same above).
    // The scale runs between round figures and its gridlines sit on them:
    // evenly spaced lines over the raw range land on odd figures, and once
    // rounded for the label they read unevenly at equal heights.
    if (ymin === ymax) ymax += 1;
    const raw = (ymax - ymin) / 3;
    const mag = 10 ** Math.floor(Math.log10(raw));
    step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((v) => v >= raw)!;
    ymin = Math.floor(ymin / step + 1e-9) * step;
    ymax = Math.ceil(ymax / step - 1e-9) * step;
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
    ? Array.from({ length: Math.round(span / step) + 1 },
                 (_v, g) => ymin + step * g)
    : Array.from(new Set([ymin, (ymin + ymax) / 2, ymax, 0])).sort((a, b) => a - b);
  // a label is printed only where it has room: two gridlines a few pixels
  // apart ($100 of checking beside the $0 line, on a scale of thousands)
  // would print one figure over the other. Zero keeps its label first.
  const labelled = new Set<number>();
  for (const gv of [...gvs].sort((a, b) => Math.abs(a) - Math.abs(b))) {
    if ([...labelled].every((k) => Math.abs(Y(k) - Y(gv)) >= 12))
      labelled.add(gv);
  }

  // which event captions fit: each needs a text line's height (rotated,
  // its width on the x axis) clear of the last one printed
  const labelledEvents = new Set<number>();
  {
    let lastX = -Infinity;
    [...events.keys()].sort((a, b) => events[a].i - events[b].i).forEach((k) => {
      const x = padL + (iw * events[k].i) / Math.max(1, n - 1);
      if (x - lastX >= 12) { labelledEvents.add(k); lastX = x; }
    });
  }
  // the legend's rows: a mark's caption goes under them, never across
  const legendRows = (second || third || band) && labels
    ? Math.min(labels.length, 1 + (second || band ? 1 : 0) + (third ? 1 : 0))
    : 0;

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
    // a series that starts late in a year puts that year's label a few
    // pixels from the next one's: the partial first year gives way
    while (xlab.length > 1 && X(xlab[1].i) - X(xlab[0].i) < 34) xlab.shift();
    // and on a narrow drawing every other year, until they fit
    while (xlab.length > 2 && iw / xlab.length < 38)
      xlab.splice(0, xlab.length, ...xlab.filter((_, k) => k % 2 === 0));
  } else {
    // as many dates as fit side by side
    const step = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(iw / 80))));
    for (let i = 0; i < n; i += step)
      xlab.push({ i, text: pretty(String(points[i][0])), tick: false });
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

  const tipAt = (i: number) => `${pretty(String(points[i][0]))}   ${money(points[i][1])}`;
  const tip = hover !== null ? tipAt(hover) : "";
  // the words: the series as drawn (the legend's names for a second and
  // third line), every figure a pick from the points
  const named: ChartSeries[] = [{ name: labels?.[0] ?? subject, points }];
  if (second) named.push({ name: labels?.[1] ?? "second line", points: second.slice(0, n) });
  if (band && !second) {
    named.push({ name: `${labels?.[1] ?? "band"} — low edge`, points: band.lo.slice(0, n) });
    named.push({ name: `${labels?.[1] ?? "band"} — high edge`, points: band.hi.slice(0, n) });
  }
  if (third) named.push({ name: labels?.[2] ?? "third line", points: third.slice(0, n) });
  // with more than one line the hovered day lists every line drawn (and
  // any the caller lists without drawing), each in its legend's words and
  // colour, its figure as given (chartsummary.hoverRows)
  const colours = [color, "var(--amber)", "var(--green)"];
  const tipSeries: ChartSeries[] = [...named, ...tipExtra];
  const multi = tipSeries.length > 1;
  const rowsAt = (i: number) => hoverRows(tipSeries, i).map((r) => ({
    ...r, colour: colours[tipSeries.findIndex((s) => s.name === r.name)] ?? "var(--mut)" }));
  const onKey = (e: React.KeyboardEvent) => {
    const i = stepPoint(e.key, hover, n);
    if (i === null) return;
    e.preventDefault();
    setHover(i);
    setSpoken(multi
      ? [pretty(String(points[i][0])), ...rowsAt(i).map((r) => `${r.name} ${money(r.value)}`),
         ...events.filter((e) => e.i === i && e.note).map((e) => e.note!)].join(", ")
      : tipAt(i).replace(/\s+/g, " "));
  };
  const tipRows = hover !== null && multi ? rowsAt(hover) : [];
  const tipLines = tipRows.map((r) => `${r.name}  ${money(r.value)}`);
  // the hovered day's events in their own words (a card due that day)
  const tipNotes = hover !== null && multi
    ? events.filter((e) => e.i === hover && e.note).map((e) => e.note!) : [];
  const bw = multi
    ? Math.max(90, ...tipLines.map((t) => t.length * 6.2 + 26),
               ...tipNotes.map((t) => t.length * 6.2 + 12))
    : Math.max(60, tip.length * 6.2 + 12);
  const bh = multi ? 18 + (tipRows.length + tipNotes.length) * 14 : 16;
  const bx = hover !== null ? Math.min(Math.max(X(hover) - bw / 2, padL), W - padR - bw) : 0;

  return (
    <>
    <svg ref={ref} viewBox={`0 0 ${W} ${H}`}
         style={{ width: "100%", display: "block" }}
         role="img" tabIndex={0}
         aria-label={chartSentence(subject, named, (v) => money(v))
                     + " Arrow keys read each point."}
         onKeyDown={onKey} onBlur={() => setHover(null)}
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
          {labelled.has(gv) && (
            <text x={padL - 6} y={Y(gv) + 3} textAnchor="end" className="ax"
                  fill="var(--mut)">{shortMoney(gv)}</text>)}
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
          {/* a label only where it has room: on a phone two autopays a
              few days apart sit closer than a line of text, and their
              captions printed over each other. The line stays; the
              timeline below names every one. */}
          {labelledEvents.has(k) && (
            <text x={X(e.i) - 3} y={padT + ih - 4} fill="var(--mut)" fontSize="9"
                  textAnchor="start"
                  transform={`rotate(-90 ${X(e.i) - 3} ${padT + ih - 4})`}>
              {e.label}</text>
          )}
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
      {band && band.lo.length > 1 && (
        <polygon fill="var(--amber)" fillOpacity="0.16" stroke="none"
          points={[...band.lo.slice(0, n).map((p, i) => `${X(i).toFixed(1)},${Y(p[1]).toFixed(1)}`),
                   ...band.hi.slice(0, n).map((p, i) => `${X(i).toFixed(1)},${Y(p[1]).toFixed(1)}`).reverse()]
            .join(" ")} />
      )}
      {second && (
        <polyline fill="none" stroke="var(--amber)" strokeWidth="2" strokeDasharray="6 4"
          points={second.slice(0, n).map((p, i) => `${X(i).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ")} />
      )}
      {third && (
        <polyline fill="none" stroke="var(--green)" strokeWidth="2" strokeDasharray="2 3"
          points={third.slice(0, n).map((p, i) => `${X(i).toFixed(1)},${Y(p[1]).toFixed(1)}`).join(" ")} />
      )}
      {(second || third || band) && labels && (() => {
        const lx0 = W - padR - Math.min(205, iw * 0.55);
        const entries: [string, string, string | undefined, string][] =
          [[color, "2.5", undefined, labels[0]]];
        if (second && labels.length > 1) entries.push(["var(--amber)", "2", "6 4", labels[1]]);
        else if (band && labels.length > 1) entries.push(["var(--amber)", "7", undefined, labels[1]]);
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
          {/* at the left, where the walk starts from today's balance:
              at the right end the line it marks is where the series
              crosses it, and the caption sat on the line */}
          <text x={padL + 4} y={Y(0) - 4} textAnchor="start" fill={RED}
                fontSize="10">$0 · out of money</text>
        </g>
      )}
      {markX !== null && markX !== undefined && markX >= 0 && markX < n && (
        <g>
          <line x1={X(markX)} y1={padT} x2={X(markX)} y2={padT + ih}
                stroke={RED} strokeWidth="1.5" strokeDasharray="5 3" />
          <circle cx={X(markX)} cy={Y(points[markX][1])} r="4.5" fill={RED} />
          {markLabel && (
            <text x={X(markX) + (markX < n / 2 ? 6 : -6)}
                  y={padT + 11 + legendRows * 14 + (legendRows ? 4 : 0)}
                  textAnchor={markX < n / 2 ? "start" : "end"} fill={RED}
                  fontSize="11" fontWeight="600">{markLabel}</text>
          )}
        </g>
      )}
      {/* one point has no line to draw: it is shown as itself */}
      {interactive && n === 1 && (
        <circle cx={pts[0][0]} cy={pts[0][1]} r="3.5" fill={color} />)}
      {!interactive && n <= 40 && pts.map(([x, y], i) => (
        <circle key={i} cx={x} cy={y} r="2.5"
                fill={negZero && points[i][1] < 0 ? RED : color} />
      ))}
      {xlab.map(({ i, text, tick }) => (
        <g key={`${i}-${text}`}>
          {tick && <line x1={X(i)} y1={padT + ih} x2={X(i)} y2={padT + ih + 4} className="cgrid" />}
          <text x={X(i)} y={H - 8} className="ax" fill="var(--mut)"
                textAnchor={X(i) > W - padR - 16 ? "end"
                  : X(i) < padL + 16 && padL < 30 ? "start" : "middle"}>{text}</text>
        </g>
      ))}
      {hover !== null && (
        <g pointerEvents="none">
          <line x1={X(hover)} x2={X(hover)} y1={padT} y2={padT + ih}
                stroke="var(--mut)" strokeOpacity=".6" strokeDasharray="3 3" />
          <circle cx={X(hover)} cy={Y(points[hover][1])} r="3.5" fill={color} />
          <rect x={bx} y={padT} width={bw} height={bh} rx="3" fill="var(--ink)" />
          {multi ? (
            <>
              <text x={bx + 6} y={padT + 11.5} fontSize="11" fill="var(--card)"
                    fontWeight="600">{pretty(String(points[hover][0]))}</text>
              {tipRows.map((r, k) => (
                <g key={r.name}>
                  <rect x={bx + 6} y={padT + 18 + k * 14} width={10} height={4}
                        fill={r.colour} />
                  <text x={bx + 20} y={padT + 24.5 + k * 14} fontSize="11"
                        fill="var(--card)">{tipLines[k]}</text>
                </g>
              ))}
              {tipNotes.map((t, k) => (
                <text key={`n${k}`} x={bx + 6}
                      y={padT + 24.5 + (tipRows.length + k) * 14} fontSize="11"
                      fill="var(--card)">{t}</text>
              ))}
            </>
          ) : (
            <text x={bx + bw / 2} y={padT + 11.5} fontSize="11" fill="var(--card)"
                  textAnchor="middle">{tip}</text>
          )}
        </g>
      )}
    </svg>
    <span className="sr-only" aria-live="polite">{spoken}</span>
    <ShowNumbers table={() => chartTable(named, (v) => money(v))} />
    </>
  );
}
