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

/** The planner's preview of the month's excess and savings while values
 *  are typed but not yet saved: the server's `plan_surplus` (income −
 *  bills − planned spending − EVERY goal's monthly plan) and
 *  `savings_plan`, each moved by the difference between what is typed and
 *  what is saved — the planned spending (`plan_variable`), the goal the
 *  planner owns (only when the excess counts it), and a typed income
 *  (only while income is typed). With nothing typed it is exactly the
 *  server's figure. With no server figure yet (no income saved) and an
 *  income box to type in, the preview is the typed income − the server's
 *  bills − the typed planned spending − every goal's plan; with no box,
 *  there is none. Twin: plannerPreview in webapp/src/moneymath.ts. */
export function plannerPreview(o: {
  planSurplus: number | null; savingsPlan: number;
  savedVariable: number; typedVariable: number;
  savedGoal: number; typedGoal: number; goalCounted: boolean;
  savedIncome: number; typedIncome: number;
  avgBills: number; incomeTyped: boolean;
}): { excess: number | null; saved: number } {
  const goal = o.goalCounted ? o.typedGoal - o.savedGoal : 0;
  const saved = o.savingsPlan + goal;
  if (o.planSurplus === null)
    return { excess: o.incomeTyped
               ? o.typedIncome - o.avgBills - o.typedVariable - saved
               : null,
             saved };
  return {
    excess: o.planSurplus - (o.typedVariable - o.savedVariable) - goal
      + (o.typedIncome - o.savedIncome),
    saved,
  };
}
