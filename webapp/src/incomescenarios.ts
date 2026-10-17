import type { IncomeScenario } from "./api/client";

// One scenario row as the card edits it. `touched` marks a row the person
// changed (or added) on this screen.
export interface ScenarioDraft {
  name: string; take_home: string; cadence: string; touched?: boolean;
}

const takeHome = (v: string) => Number(v.replace(/[$,\s]/g, ""));

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
    out.push({ name, take_home: takeHome(r.take_home), cadence: r.cadence });
  }
  for (const s of current)
    if (!baseNames.has(key(s.name)) && !has(s.name))
      out.push(s);                                // added elsewhere since
  return out;
}
