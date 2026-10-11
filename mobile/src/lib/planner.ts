// The budget planner's arithmetic, pure so the logic tests can hold it.

/** "Everything else" before the person edits it: the variable pool less
 *  the carve-outs, less the savings typed in THIS session. A Savings plan
 *  that was already saved sits beside other_monthly, not inside it (the
 *  saved other_monthly is already net of it), so only the change from the
 *  saved plan comes out of the pool — taking the whole plan out again on
 *  every reopen would cut the stored budget by that much on each save. */
export function plannerUnallocAuto(o: {
  otherBase: number; catsTotal: number; savingsTouched: boolean;
  savingsTyped: number; savingsSeed: number;
}): number {
  const taken = o.savingsTouched ? o.savingsTyped - o.savingsSeed : 0;
  return Math.max(0, Math.round(o.otherBase - o.catsTotal - taken));
}

/** The money map's Savings row figure for a month. The goal's
 *  `saved_this_month` is what was posted toward the plan this month; an
 *  explicit `month` (a lens payload's own figure) wins over it. Without
 *  either the only figure is the goal's trailing 90-day average, and the
 *  row must say that is what it shows rather than call it this month's
 *  transfers. A plan-only goal (rate_90d === null) stays null. */
export function monthSavings(
  goal: { monthly_plan: number; rate_90d: number | null;
          saved_this_month?: number | null },
  month?: number | null,
): { plan: number; saved: number | null; averaged: boolean } {
  const posted = month ?? goal.saved_this_month;
  if (typeof posted === "number" && Number.isFinite(posted))
    return { plan: goal.monthly_plan, saved: posted, averaged: false };
  return { plan: goal.monthly_plan, saved: goal.rate_90d,
           averaged: goal.rate_90d !== null };
}

export const AVERAGED_SAVINGS_CAPTION = "avg / mo, last 90 days";

/** The plan rows with the Savings row's caption made honest about an
 *  averaged figure. */
export function labelSavingsRow<T extends { label: string; caption?: string }>(
  rows: T[], averaged: boolean, caption: string = AVERAGED_SAVINGS_CAPTION,
): T[] {
  if (!averaged) return rows;
  return rows.map((r) => r.label === "Savings / Investment" && r.caption
    ? { ...r, caption } : r);
}

export const ESTIMATED_SAVINGS_CAPTION = "estimate from the last 90 days";

/** The year map's Savings row: the standing monthly plan times the months
 *  the other rows cover. The lens's `saved_ytd` is the ledger's own figure
 *  for those months; without it (an older server, or no budgeted month to
 *  measure) the only number is the 90-day rate times the same months, which
 *  is a projection, so the row must say it is an estimate rather than call
 *  it what was transferred. Null when there is no goal or no month yet. */
export function yearSavings(
  goal: { monthly_plan: number; rate_90d: number | null } | null | undefined,
  savedYtd: number | null | undefined,
  elapsed: number,
): { plan: number; saved: number | null; estimated: boolean } | null {
  if (!goal || !(elapsed > 0)) return null;
  const plan = goal.monthly_plan * elapsed;
  if (typeof savedYtd === "number" && Number.isFinite(savedYtd))
    return { plan, saved: savedYtd, estimated: false };
  if (goal.rate_90d === null) return { plan, saved: null, estimated: false };
  return { plan, saved: goal.rate_90d * elapsed, estimated: true };
}
