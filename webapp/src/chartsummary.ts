// What a chart says in words: one sentence (where the line starts and
// ends, and its highest and lowest point when those are elsewhere) and
// the table behind a "Show numbers" button. Both are picks from the
// points the chart already draws, printed with the chart's own money
// format — nothing is added up or derived here, so the words can never
// disagree with the picture. The phone's copy is
// mobile/src/lib/chart-summary.ts; one case table runs against both.
// Design: specs/accessibility.md, "Chart text alternatives".

export type ChartPoint = readonly [string, number | null];
export interface ChartSeries { name: string; points: readonly ChartPoint[] }
export interface ChartTable { columns: string[]; rows: string[][] }

const MONTHS = ["January", "February", "March", "April", "May", "June", "July",
                "August", "September", "October", "November", "December"];
const ISO = /^(\d{4})-(\d{2})(?:-(\d{2}))?$/;

/** A point's label in words: "2026-11" is "November 2026", "2026-11-14"
 *  is "November 14, 2026"; without the year, "November" and
 *  "November 14". A year, a month name or any other label stays as it is. */
export function pointName(label: string, withYear = true): string {
  const m = ISO.exec(label);
  const mon = m ? MONTHS[Number(m[2]) - 1] : undefined;
  if (!m || !mon) return label;
  const day = m[3] ? ` ${Number(m[3])}` : "";
  if (!withYear) return mon + day;
  return m[3] ? `${mon}${day}, ${m[1]}` : `${mon} ${m[1]}`;
}

const preposition = (label: string) => (/^\d{4}-\d{2}-\d{2}$/.test(label) ? "on" : "in");

/** One series in a sentence: "Net worth: $41,200 in December 2025,
 *  $48,900 in November 2026; highest $49,300 in October." The highest
 *  and lowest are named only when they are not the start or the end; they
 *  drop the year when no other point shares their month (or day). A
 *  point from `forecastFrom` on is marked "(forecast)"; a null (a month
 *  nothing measured) is skipped. */
export function seriesSentence(subject: string, points: readonly ChartPoint[],
                               fmt: (v: number) => string,
                               forecastFrom?: number): string {
  const live = points.flatMap(([label, v], i) => (v === null ? [] : [{ label, v, i }]));
  if (!live.length) return `${subject}: no figures yet.`;
  const ff = forecastFrom ?? points.length;
  const shortCount = new Map<string, number>();
  for (const [label] of points) {
    const s = pointName(label, false);
    shortCount.set(s, (shortCount.get(s) ?? 0) + 1);
  }
  const say = (p: { label: string; v: number; i: number }, short: boolean) => {
    const bare = pointName(p.label, false);
    const name = short && bare !== p.label && shortCount.get(bare) === 1
      ? bare : pointName(p.label);
    return `${fmt(p.v)} ${preposition(p.label)} ${name}${p.i >= ff ? " (forecast)" : ""}`;
  };
  const first = live[0], last = live[live.length - 1];
  if (live.length === 1) return `${subject}: ${say(first, false)}.`;
  let hi = first, lo = first;
  for (const p of live) {
    if (p.v > hi.v) hi = p;
    if (p.v < lo.v) lo = p;
  }
  const ends = new Set([first.v, last.v]);
  const extra: string[] = [];
  if (!ends.has(hi.v)) extra.push(`highest ${say(hi, true)}`);
  if (!ends.has(lo.v)) extra.push(`lowest ${say(lo, true)}`);
  return `${subject}: ${say(first, false)}, ${say(last, false)}`
    + (extra.length ? `; ${extra.join("; ")}` : "") + ".";
}

/** Several series: the first under the chart's subject, each other under
 *  its own name. */
export function chartSentence(subject: string, series: readonly ChartSeries[],
                              fmt: (v: number) => string, forecastFrom?: number): string {
  return series.map((s, k) => seriesSentence(k === 0 ? subject : s.name, s.points, fmt,
                                             forecastFrom)).join(" ");
}

/** A long run of daily points is listed by the last day of each month
 *  (the table would otherwise be thousands of rows); anything else in
 *  full. Index picks only. */
export function tableIndexes(points: readonly ChartPoint[], max = 62): number[] {
  const all = points.map((_, i) => i);
  if (points.length <= max || !points.every(([l]) => /^\d{4}-\d{2}-\d{2}$/.test(l)))
    return all;
  return all.filter((i) => i === points.length - 1
    || points[i + 1][0].slice(0, 7) !== points[i][0].slice(0, 7));
}

/** The "Show numbers" table: a row per point, a column per series. */
export function chartTable(series: readonly ChartSeries[], fmt: (v: number) => string,
                           opts: { forecastFrom?: number; missing?: string } = {}): ChartTable {
  const lead = series[0]?.points ?? [];
  const daily = lead.length > 0 && lead.every(([l]) => /^\d{4}-\d{2}-\d{2}$/.test(l));
  const monthly = lead.length > 0 && lead.every(([l]) => /^\d{4}-\d{2}$/.test(l));
  const ff = opts.forecastFrom ?? lead.length;
  return {
    columns: [daily ? "Date" : monthly ? "Month" : "Point", ...series.map((s) => s.name)],
    rows: tableIndexes(lead).map((i) => [
      pointName(lead[i][0]) + (i >= ff ? " (forecast)" : ""),
      ...series.map((s) => {
        const v = s.points[i]?.[1];
        return v === null || v === undefined ? (opts.missing ?? "—") : fmt(v);
      })]),
  };
}

/** Parts of a whole in words, one clause per group: "Came in: Paychecks
 *  $5,000 (83%), Interest & dividends $1,000 (17%). Went out: …". The
 *  figures arrive printed — the caller passes the server's shown ones. */
export function partsSentence(groups: readonly { head: string;
                                                 parts: readonly { label: string; figure: string;
                                                                   share?: number | null }[] }[]):
    string {
  return groups.filter((g) => g.parts.length)
    .map((g) => `${g.head}: ${g.parts.map((p) => `${p.label} ${p.figure}`
      + (p.share === null || p.share === undefined ? "" : ` (${p.share}%)`)).join(", ")}.`)
    .join(" ");
}

/** Arrow keys walk a chart's points the way the mouse moves its tooltip: Left/Right
 *  (and Down/Up) one point, Home and End the ends. Returns the next index,
 *  or null when the key is not one of them. */
export function stepPoint(key: string, at: number | null, n: number): number | null {
  if (!n) return null;
  if (key === "Home") return 0;
  if (key === "End") return n - 1;
  if (key === "ArrowLeft" || key === "ArrowDown")
    return at === null ? n - 1 : Math.max(0, at - 1);
  if (key === "ArrowRight" || key === "ArrowUp")
    return at === null ? 0 : Math.min(n - 1, at + 1);
  return null;
}

/** The hovered point on every line the chart draws, in the order drawn:
 *  one row per series with a figure at index `i`, its name and its value
 *  as given (the chart's tooltip prints these; nothing is derived). */
export function hoverRows(series: readonly ChartSeries[],
                          i: number): { name: string; value: number }[] {
  const out: { name: string; value: number }[] = [];
  for (const s of series) {
    const v = s.points[i]?.[1];
    if (v !== null && v !== undefined) out.push({ name: s.name, value: v });
  }
  return out;
}
