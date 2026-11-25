// The two pictures on the Cash Flow page — shared math with the mobile
// twins in mobile/src/lib/flowlayout.ts / mobile/src/components/flow-*.tsx:
//   • FlowChart: where the money came from (paychecks, interest, other)
//     and where it went (fixed bills, variable spend, saved), band widths
//     proportional to dollars. When the period overspent, an "overspent"
//     source on the left makes up the difference so the two sides balance.
//   • NetBars: saved (or overspent) per month, the amount written on every
//     bar so the number is read, not estimated off an axis; the forecast
//     tail is drawn hollow.
import { useRef } from "react";
import { useFitWidth } from "./useFitWidth";
import type { FlowPeriod } from "../api/client";
import { money } from "../api/client";
import { chartSentence, chartTable, partsSentence } from "../chartsummary";
import { netBarsLabelWidth, netBarsShowValues } from "../moneymath";
import { ShowNumbers } from "./ChartText";

export const k$ = (v: number): string => {
  const a = Math.abs(v);
  if (a >= 1_000_000) return `${v < 0 ? "-" : ""}$${(a / 1_000_000).toFixed(2)}m`;
  if (a >= 10_000) return `${v < 0 ? "-" : ""}$${Math.round(a / 1000)}k`;
  if (a >= 1000) return `${v < 0 ? "-" : ""}$${(a / 1000).toFixed(1).replace(/\.0$/, "")}k`;
  return money(v);
};

const COL = { paychecks: "#4f9bd6", interest: "#7fb8e6", other: "#3d7fb0",
              overspent: "#e0695d", fixed: "#e0695d", variable: "#f0b429",
              saved: "#5cb56b", unsplit: "#9aa3ad" };

export interface FlowNode { key: string; label: string; value: number; color: string }

/** The layout the SVG draws — pure, so the mobile twin runs the same
 *  function over the same numbers. `L` is the balanced total. */
export function flowLayout(f: FlowPeriod) {
  // unknown income (no bank feed covers the window) draws no income
  // side and no saved/overspent node — nothing to balance against
  const inTotal = f.in.total ?? 0, outTotal = f.out.total;
  const saved = f.saved ?? 0;
  const sources: FlowNode[] = [
    { key: "paychecks", label: "Paychecks", value: f.in.paychecks ?? 0, color: COL.paychecks },
    { key: "interest", label: "Interest & dividends", value: f.in.interest ?? 0, color: COL.interest },
    { key: "other", label: "Other income", value: f.in.other ?? 0, color: COL.other },
  ];
  if (saved < 0) sources.push({ key: "overspent", label: "Overspent by",
                                value: -saved, color: COL.overspent });
  const sinks: FlowNode[] = [
    { key: "fixed", label: "Bills & envelopes", value: f.out.fixed, color: COL.fixed },
    // the server's split (reporting._flow_split): variable over the
    // months a bill schedule reached, and the months none did apart
    { key: "variable", label: "Variable spend", value: f.out.variable_scheduled,
      color: COL.variable },
    { key: "unsplit", label: "Before any bill", value: f.out.unsplit,
      color: COL.unsplit },
  ];
  if (saved > 0) sinks.push({ key: "saved", label: "Left over",
                              value: saved, color: COL.saved });
  const L = Math.max(inTotal, outTotal, 0);
  return { sources: sources.filter((n) => n.value > 0),
           sinks: sinks.filter((n) => n.value > 0), L };
}

export function FlowChart({ flow, width: maxW = 720, height = 250 }: {
  flow: FlowPeriod; width?: number; height?: number;
}) {
  // drawn at the width of its card: scaled down from 720 a phone got
  // labels a few pixels tall
  const svgRef = useRef<SVGSVGElement>(null);
  const drawn = !(flow.income_unknown || flow.in.total === null);
  const width = useFitWidth(svgRef, maxW, drawn);
  // too narrow for a name and its figures on both sides of one band: the
  // picture keeps the names, and every figure is listed under it
  const narrow = width < 480;
  // unknown income (no bank feed covers the window): the story line
  // under the picture says so and gives the spend — an empty-picture
  // sentence here would claim nothing went out either
  if (flow.income_unknown || flow.in.total === null) return null;
  const { sources, sinks, L } = flowLayout(flow);
  if (!L || !sources.length || !sinks.length)
    return <p className="mut" style={{ margin: ".8rem 0" }}>
      No income or spending in this period yet.</p>;
  const pad = 8, gap = 10, nodeW = 14, top = 14;
  const usable = height - top - pad - gap * (Math.max(sources.length, sinks.length) - 1);
  const scale = usable / L;
  // stack the nodes, remembering each one's running offset for the bands
  const place = (nodes: FlowNode[]) => {
    let y = top;
    return nodes.map((n) => {
      const h = Math.max(n.value * scale, 2);
      const o = { ...n, y, h, cursor: y };
      y += h + gap;
      return o;
    });
  };
  const S = place(sources), K = place(sinks);
  const x0 = nodeW, x1 = width - nodeW;
  const bands: { d: string; color: string }[] = [];
  for (const s of S) {
    for (const k of K) {
      const v = s.value * k.value / L;
      if (v <= 0) continue;
      const h = v * scale;
      const y0 = s.cursor, y1 = k.cursor;
      s.cursor += h; k.cursor += h;
      const cx = (x0 + x1) / 2;
      bands.push({ color: k.color,
        d: `M${x0},${y0} C${cx},${y0} ${cx},${y1} ${x1},${y1} L${x1},${y1 + h} `
         + `C${cx},${y1 + h} ${cx},${y0 + h} ${x0},${y0 + h} Z` });
    }
  }
  // each node's figures as printed are the server's (flow_breakdown →
  // in.shown / out.shown): a side's dollars add up to its total and its
  // shares to 100; the geometry above keeps the exact values
  const shownOf = new Map<string, { dollars: number; share: number | null }>(
    [...(flow.in.shown ?? []), ...flow.out.split_shown].map((n) => [n.key, n]));
  const dollars = (n: FlowNode) => {
    const d = shownOf.get(n.key);
    return d ? money(d.dollars) : "";
  };
  const pct = (n: FlowNode) => {
    const p = shownOf.get(n.key)?.share;
    return p == null ? "" : ` · ${p}%`;
  };
  const textY = (n: { y: number; h: number }) => n.y + n.h / 2 + 4;
  // a sliver too thin to carry its own label is named under the picture
  // instead — its label would otherwise sit on the neighbour's band
  const inline = (n: { h: number }) => n.h >= 16;
  const listed = (n: { h: number }) => narrow || !inline(n);
  const small = [...S.filter(listed), ...K.filter(listed)];
  // the picture in words: every node with the dollars and share it prints
  const parts = (nodes: FlowNode[]) => nodes.map((n) => ({
    label: n.label, figure: dollars(n), share: shownOf.get(n.key)?.share ?? null }));
  const sides = [{ head: "Where the money came from", parts: parts(sources) },
                 { head: "Where it went", parts: parts(sinks) }];
  return (
    <>
      <svg ref={svgRef} viewBox={`0 0 ${width} ${height}`} width="100%" style={{ display: "block" }}
           role="img" aria-label={partsSentence(sides)}>
        {bands.map((b, i) => <path key={i} d={b.d} fill={b.color} opacity={.28} />)}
        {S.map((n) => <rect key={n.key} x={0} y={n.y} width={nodeW} height={n.h} rx={2}
                            fill={n.color} />)}
        {K.map((n) => <rect key={n.key} x={x1} y={n.y} width={nodeW} height={n.h} rx={2}
                            fill={n.color} />)}
        {S.filter(inline).map((n) => (
          <text key={n.key} x={nodeW + 8} y={textY(n)} fontSize={12} fill="var(--ink)">
            {n.label}{!narrow && <tspan fill="var(--mut)" fontSize={11}>
              {"  "}{dollars(n)}{pct(n)}</tspan>}
          </text>))}
        {K.filter(inline).map((n) => (
          <text key={n.key} x={x1 - 8} y={textY(n)} fontSize={12} fill="var(--ink)"
                textAnchor="end">
            {n.label}{!narrow && <tspan fill="var(--mut)" fontSize={11}>
              {"  "}{dollars(n)}{pct(n)}</tspan>}
          </text>))}
      </svg>
      {small.length > 0 && (
        <div className="sub" style={{ marginTop: ".2rem" }}>
          {small.map((n) => (
            <span key={n.key} style={{ display: "inline-block",
                                       whiteSpace: "nowrap", marginRight: ".8rem" }}>
              <span style={{ display: "inline-block", width: 8, height: 8, borderRadius: 2,
                             background: n.color, marginRight: 4, verticalAlign: -1 }} />
              {n.label} {dollars(n)}{pct(n)}
            </span>))}
        </div>)}
      <ShowNumbers table={() => ({
        columns: ["Part", "Dollars", "Share"],
        rows: sides.flatMap((g) => g.parts.map((p) => [
          p.label, p.figure, p.share === null ? "" : `${p.share}%`])),
      })} />
    </>
  );
}

/** Saved per month with the number on every bar. `estimatedFrom` is the
 *  index where the forecast starts (drawn hollow). Above ~14 bars only every
 *  nth label is written, or they would overprint each other. */
export function NetBars({ points, estimatedFrom, height = 190, width: maxW = 720,
                          posColor = "#5cb56b", subject = "Left over per month",
                          missing = "no bank feed" }: {
  // a null is a month no bank feed covers: its slot stays, empty
  points: [string, number | null][]; estimatedFrom?: number; height?: number;
  width?: number;
  /** what an empty slot is called, in its tooltip and the numbers table
   *  (Cash Flow passes the server's words for an incomplete month) */
  missing?: string;
  // green by default (saved); the Income tab passes the flow picture's
  // income blue so "money in" keeps one color everywhere
  posColor?: string;
  /** what the bars are, for the sentence and the "Show numbers" table */
  subject?: string;
}) {
  const svgRef = useRef<SVGSVGElement>(null);
  const width = useFitWidth(svgRef, maxW, points.length === 0);
  if (!points.length) return null;
  const n = points.length;
  const maxAbs = Math.max(1, ...points.map((p) => Math.abs(p[1] ?? 0)));
  const labelTop = 16, labelBot = 18, axis = 16;
  const plotH = height - labelTop - labelBot - axis;
  // the zero line sits mid-plot: a month can save or overspend up to
  // maxAbs either way, and the eye compares bar lengths across the line
  const zero = labelTop + plotH / 2;
  const slot = width / n;
  const barW = Math.max(3, Math.min(34, slot * 0.62));
  // a label needs its own width of slot to itself: about 34px for a bare
  // month, more when it carries its year ("Mar '24")
  const labelW = netBarsLabelWidth(points.map((p) => p[0]), 6.2, 34);
  const every = Math.max(n <= 14 ? 1 : Math.ceil(n / 14),
                         Math.ceil(labelW / slot));
  // the amount is written on a bar only while bars are wide enough to
  // carry one: over hundreds of thin bars it lands on its neighbours, and
  // the numbers are in the hover and under "Show numbers"
  const values = netBarsShowValues(slot);
  // a label is kept whole inside the drawing: the last one is not cut
  const labelX = (x: number) =>
    Math.max(labelW / 2, Math.min(width - labelW / 2, x + barW / 2));
  const ef = estimatedFrom ?? n;
  const named = [{ name: subject, points }];
  return (
    <>
    <svg ref={svgRef} viewBox={`0 0 ${width} ${height}`} width="100%" style={{ display: "block" }}
         role="img" aria-label={chartSentence(subject, named, (v) => money(v), ef)}>
      <line x1={0} x2={width} y1={zero} y2={zero} stroke="var(--line)" />
      {points.map(([label, v], i) => {
        const x = slot * i + (slot - barW) / 2;
        const show = (n - 1 - i) % every === 0;
        if (v === null) {
          return (
            <g key={`${label}-${i}`}>
              <title>{label}: {missing}</title>
              <text x={x + barW / 2} y={zero - 4} fontSize={10.5}
                    textAnchor="middle" fill="var(--mut)">—</text>
              {show && (
                <text x={labelX(x)} y={height - 4} fontSize={10.5}
                      textAnchor="middle" fill="var(--mut)">{label}</text>)}
            </g>
          );
        }
        const h = Math.abs(v) / maxAbs * (plotH / 2);
        const y = v >= 0 ? zero - h : zero;
        const fc = i >= ef;
        const color = v >= 0 ? posColor : "#e0695d";
        return (
          <g key={`${label}-${i}`}>
            <title>{label}: {money(v)}{fc ? " (forecast)" : ""}</title>
            <rect x={x} y={y} width={barW} height={Math.max(h, 1)} rx={3}
                  fill={fc ? "none" : color} stroke={color}
                  strokeDasharray={fc ? "3 3" : undefined} opacity={fc ? .7 : .9} />
            {show && values && (
              <text x={Math.max(16, Math.min(width - 16, x + barW / 2))}
                    y={v >= 0 ? y - 4 : y + h + 12} fontSize={10.5}
                    textAnchor="middle" fill={fc ? "var(--mut)" : color}
                    fontVariant="tabular-nums">{k$(v)}</text>)}
            {show && (
              <text x={labelX(x)} y={height - 4} fontSize={10.5} textAnchor="middle"
                    fill="var(--mut)">{label}</text>)}
          </g>
        );
      })}
    </svg>
    <ShowNumbers table={() => chartTable(named, (v) => money(v),
                                         { forecastFrom: ef, missing })} />
    </>
  );
}
