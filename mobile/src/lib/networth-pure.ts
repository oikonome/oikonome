// Net worth page math, runnable under plain node (scripts/pure-logic-tests.mjs).

/** The change from the last month-end to now on a monthly series
 *  ([YYYY-MM, value] pairs, the current month last), or null when the
 *  series has no earlier month — or is absent, as from a server that does
 *  not send it. The hero passes the full series (property included) so the
 *  change is measured on the base of the Total net worth beside it; the
 *  Financial accounts line passes the financial-only `trend`. No fallback
 *  from one to the other: a financial change shown beside the total would
 *  read a mortgage payment as a full loss. */
export function monthChange(
  series: [string, number][] | null | undefined,
): number | null {
  if (!series || series.length < 2) return null;
  return series[series.length - 1][1] - series[series.length - 2][1];
}
