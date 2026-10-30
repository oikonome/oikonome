import type { IncomeScenario } from "./api/client";
import { parseMoneyDraft } from "./moneymath.ts";

// One scenario row as the card edits it. `touched` marks a row the person
// changed (or added) on this screen.
export interface ScenarioDraft {
  name: string; take_home: string; cadence: string; touched?: boolean;
}

/** A typed take-home by the server's separator rules ("12,50" is
 *  twelve-fifty, "1,234.56" a thousands comma). Blank is 0; text that is
 *  not a number is NaN — fine for the live preview, never for a save:
 *  NaN travels as JSON null (see mergeIncomeScenarios). */
export const takeHomeOf = (v: string) =>
  v.replace(/[$\s]/g, "") === "" ? 0 : parseMoneyDraft(v) ?? NaN;

/**
 * The income_scenarios list to save, given the card's rows, the list the
 * card was seeded from (`baseline`) and the server's list read just
 * before saving (`current`).
 *
 * The server replaces the whole list, so a save built only from the
 * seeded copy would erase whatever another device or member saved since.
 * Invariant: a row edited here wins; an untouched row takes its live
 * server value; an untouched row that the server no longer has (and was
 * in the baseline) was deleted elsewhere and stays deleted; a row the
 * person removed here stays removed; a scenario added elsewhere since the
 * baseline is carried through. Names compare case-insensitively, the
 * same way the server rejects duplicates.
 *
 * A take-home typed here that is not an amount throws, naming the row, so
 * the card shows it and nothing is sent: NaN would reach the server as
 * JSON null and be read as no figure at all.
 *
 * The mobile client mirrors this rule; keep the two in step.
 */
export function mergeIncomeScenarios(rows: ScenarioDraft[],
                                     baseline: IncomeScenario[],
                                     current: IncomeScenario[]): IncomeScenario[] {
  const key = (n: string) => n.trim().toLowerCase();
  const baseNames = new Set(baseline.map((s) => key(s.name)));
  const curByName = new Map(current.map((s) => [key(s.name), s]));
  const out: IncomeScenario[] = [];
  const has = (n: string) => out.some((o) => key(o.name) === key(n));
  for (const r of rows) {
    const name = r.name.trim();
    if (!name) continue;
    if (!r.touched) {
      const live = curByName.get(key(name));
      if (live) { out.push(live); continue; }
      if (baseNames.has(key(name))) continue;   // deleted elsewhere
    }
    const take_home = takeHomeOf(r.take_home);
    if (!Number.isFinite(take_home))
      throw new Error(`${name}: "${r.take_home.trim()}" is not an amount — `
                      + "type the take-home like 2,100.50.");
    out.push({ name, take_home, cadence: r.cadence });
  }
  for (const s of current)
    if (!baseNames.has(key(s.name)) && !has(s.name))
      out.push(s);                                // added elsewhere since
  return out;
}
