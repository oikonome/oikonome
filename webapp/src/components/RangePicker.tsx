// The site-wide timeframe vocabulary. Every over-time toggle offers the
// same six windows — 3m 6m 1y 3y 5y All — so a reader never relearns a
// picker per page (Net Worth set the convention; Cash Flow and the rest
// follow it). The mobile twin of the constants is mobile/src/lib/pure.ts
// (TREND_RANGES) — keep them identical.
export const RANGES = ["3m", "6m", "1y", "3y", "5y", "all"] as const;
// Cash Flow adds two short windows in front of the six: "This month" (the
// household's month so far) and "1m" (the last complete month). They are
// months on the same clock, cut server-side like the rest
// (reporting.FLOW_RANGES), and only the cash-flow surfaces — overview,
// spending, income — offer them; a net-worth series has no use for a
// window one point long.
export const CASHFLOW_RANGES = ["cur", "1m", ...RANGES] as const;
export type Range = (typeof CASHFLOW_RANGES)[number];
export const RANGE_LABEL: Record<Range, string> =
  { cur: "This month", "1m": "1m", "3m": "3m", "6m": "6m", "1y": "1y",
    "3y": "3y", "5y": "5y", all: "All" };
// months back from the household's current month, inclusive. "1m" counts
// two so a window cut on the current month reaches the last complete one
// — the cash graph's history stops before the current month, so the two
// short windows differ by whether the current month is in the picture.
export const RANGE_MONTHS: Record<Range, number | null> =
  { cur: 1, "1m": 2, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60, all: null };

// ---- counting months backwards, safely ----
// Date#setUTCMonth keeps the day-of-month, so shifting a 31st back into a
// 30-day month rolls FORWARD into the next one (Mar 31 − 1 month = Mar 3).
// Every window built that way silently loses its oldest month on the last
// days of a long month. Both helpers below anchor or clamp the day first,
// which is what the server does (engine/reporting.py `_range_window`).
// Mobile's twins are `rangeCutoffMonth`/`shiftMonthsUtc` in
// mobile/src/lib/pure.ts — keep them identical.

/** The first month a range's window includes, as "YYYY-MM", counted back
 *  from `today`. null for "all", which has no cutoff. */
export function rangeCutoffMonth(range: Range, today: Date = new Date()):
    string | null {
  const months = RANGE_MONTHS[range];
  if (months === null) return null;
  return monthsBackFrom(
    // anchored to the 1st, so the day-of-month cannot overflow
    new Date(Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), 1))
      .toISOString().slice(0, 7),
    months);
}

/** The first month of a `months`-long window whose LAST month is `anchor`
 *  ("YYYY-MM"). A 3-month window ending 2026-08 starts 2026-06. */
export function monthsBackFrom(anchor: string, months: number): string {
  const d = new Date(Date.UTC(Number(anchor.slice(0, 4)),
                              Number(anchor.slice(5, 7)) - 1, 1));
  d.setUTCMonth(d.getUTCMonth() - (months - 1));
  return d.toISOString().slice(0, 7);
}

// ---- whose "now" a window is cut on ----
// The browser's clock is not the household's. Every figure on the cash-flow
// page is windowed server-side from the household's own date
// (localtime.now_local → reporting._range_window), and a reader in another
// timezone is in a different month for hours at each month boundary: a
// household in Los Angeles at 6pm on Aug 31 is still in August while the
// browser's UTC month is already September. Cutting the chart on the
// browser would drop June from a "3m" window whose server figures cover
// Jun+Jul+Aug — the best/worst caption would be computed over a window one
// month shorter than the one being read.
//
// The report itself names the household's month: the cash graph's history
// stops BEFORE the current month and the forecast tail opens ON it, so the
// first forecast point is the household's today, straight from the server.

/** The household's current month ("YYYY-MM") as the cash graph names it —
 *  its first forecast point. null when the report carries no forecast
 *  tail, which is the only case with nothing in it to anchor on. */
export function graphCurrentMonth(
  graph: { points: [string, number][]; forecast_from: number },
): string | null {
  return graph.points[graph.forecast_from]?.[0] ?? null;
}

/** The first month a cash-flow range covers, anchored on the household's
 *  month rather than the browser's. `today` is only the fallback for a
 *  report with no forecast tail to read the household's month from.
 *  Mobile's twin is `cashflowCutoffMonth` in mobile/src/lib/pure.ts. */
export function cashflowCutoffMonth(
  graph: { points: [string, number][]; forecast_from: number },
  range: Range, today: Date = new Date(),
): string | null {
  const months = RANGE_MONTHS[range];
  if (months === null) return null;
  const cur = graphCurrentMonth(graph);
  return cur ? monthsBackFrom(cur, months) : rangeCutoffMonth(range, today);
}

/** `iso` moved `months` months earlier, in the shape it came in: a
 *  "YYYY-MM-DD" keeps its day, clamped to the target month's length (May 31
 *  − 3 months is Feb 28, not Mar 3); a "YYYY-MM" month key — the net-worth
 *  trend's points — stays a month key. A month key has no day to read, and
 *  treating the missing day as a number makes an invalid Date whose
 *  toISOString() throws, taking the whole Net Worth screen down. */
export function shiftMonthsUtc(iso: string, months: number): string {
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  if (!Number.isFinite(d)) {
    return new Date(Date.UTC(y, m - 1 - months, 1)).toISOString().slice(0, 7);
  }
  // day 0 of the month AFTER the target = the target's last day
  const last = new Date(Date.UTC(y, m - months, 0)).getUTCDate();
  return new Date(Date.UTC(y, m - 1 - months, Math.min(d, last)))
    .toISOString().slice(0, 10);
}

/** The ledger's quick date filter for a range: the first day of the
 *  window's opening month as a LOCAL "YYYY-MM-DD" ("3m" in September is
 *  Jul 1 — the same three calendar months Cash Flow's 3m covers), "" for
 *  "all". A calendar date the reader types into a date box is local, so
 *  this one is cut on the browser's calendar, not UTC. The mobile twin is
 *  `ledgerRange` in mobile/src/lib/pure.ts — keep them identical. */
export function ledgerRangeFrom(range: Range, today: Date = new Date()): string {
  const months = RANGE_MONTHS[range];
  if (months === null) return "";
  // Date's own month arithmetic on the 1st: no day-of-month to overflow
  const d = new Date(today.getFullYear(), today.getMonth() - (months - 1), 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-01`;
}

export function RangePicker({ value, onChange, dead, options }: {
  value: Range; onChange: (r: Range) => void;
  // a range longer than the data equals All — the caller can mark it so
  // the picker doesn't draw two buttons for one view
  dead?: (r: Range) => boolean;
  // the six site-wide windows by default; Cash Flow passes CASHFLOW_RANGES
  options?: readonly Range[];
}) {
  return (
    <span style={{ display: "inline-flex", gap: ".35rem", flexWrap: "wrap" }}>
      {(options ?? RANGES).map((r) => {
        const d = r !== "all" && !!dead?.(r);
        return (
          <button key={r} className={`pill m${value === r ? " pri" : ""}`}
                  disabled={d} style={d ? { opacity: .35 } : undefined}
                  onClick={() => onChange(r)}>
            {RANGE_LABEL[r]}</button>
        );
      })}
    </span>
  );
}
