// Business-page rules and wizard routing, kept free of React so the same
// rules can be exercised by a plain-node test. The phone carries an
// identical copy in mobile/src/lib/pure.ts, and the mobile pure-logic suite
// runs both copies over the same cases — the two clients must never open
// the wizard on a different business. The page's figures (this month's
// profit, the cash in the business, the tax set-aside) are the server's.

/** An account's balance belongs in a money aggregate.
 *
 *  Two kinds of row carry a balance that is not money to add up:
 *  - the backup side of a linked pair — one real account fed by two
 *    sources, which the server's own sums count once;
 *  - a hidden account. Hiding a manual or imported account keeps its last
 *    balance (there is no feed to restore it on unhide), so a screen that
 *    sums balances itself would still count money the owner switched off. */
export const countsInTotals = (a: {
  link?: { primary: boolean } | null; user_removed_at?: string | null }) =>
  (!a.link || a.link.primary) && !a.user_removed_at;

/** Which existing business a guided-setup run continues.
 *
 *  The caller's choice when it names one that is still active; otherwise
 *  the oldest active business, so a bare "Business wizard" never offers to
 *  create a second one next to a business that exists. An archived business
 *  is closed and read-only, so it is never continued — with nothing active
 *  the run starts fresh. */
export function bizSetupEntity<E extends { id: string; status?: string | null }>(
  entities: readonly E[], wanted?: string | null,
): E | undefined {
  const active = entities.filter((e) => e.status === "active");
  return (wanted ? active.find((e) => e.id === wanted) : undefined)
    ?? active[0];
}

/** The step the business wizard opens at.
 *
 *  With a business in hand: its accounts step, the first one that needs
 *  the entity. Without one: the first question, whatever the step marks
 *  say. The marks are household-wide and outlive the business they were
 *  written for (a deleted one, or another business altogether), and every
 *  step after the questions writes against the entity — opened with none,
 *  the accounts step would "assign" accounts back to personal and report
 *  success. Nor can a run resume between the two questions: their answers
 *  live in the screen until the entity is created at the end of the second,
 *  so reopening there would ask for dates with the name already gone. */
export function bizWizardEntryStep(
  steps: readonly { key: string }[], hasEntity: boolean,
): number {
  return hasEntity ? steps.findIndex((s) => s.key === "accounts") : 0;
}

/** The tax years the year-end package and the 1099 list offer, newest
 *  first: this year back to the year the business was formed or started,
 *  whichever is earlier (pre-opening costs sit in the formation year), at
 *  most six; three when neither date is known, the same reach as the
 *  P&L's own year list. */
export function bizTaxYears(
  today: Date, formed: string | null | undefined,
  started: string | null | undefined,
): number[] {
  const now = today.getFullYear();
  const known = [formed, started]
    .map((d) => Number((d || "").slice(0, 4)))
    .filter((y) => y > 1900 && y <= now);
  const oldest = known.length ? Math.max(Math.min(...known), now - 5)
    : now - 2;
  const out: number[] = [];
  for (let y = now; y >= oldest; y--) out.push(y);
  return out;
}

/** The year a tax control opens on. From January through April last
 *  year's return is the one being filed (the accountant's package, the
 *  1099-NEC due January 31), so it opens on last year when the business
 *  had one; otherwise on this year. */
export function bizDefaultTaxYear(
  today: Date, years: readonly number[],
): number {
  const now = today.getFullYear();
  return today.getMonth() <= 3 && years.includes(now - 1) ? now - 1 : now;
}

/** The bucket a business cost shows on the Books control: the one the
 *  server's P&L counts it in (a cost dated before the business started is
 *  start-up there), else the stored class. A control that showed
 *  "Operating" for a start-up cost could not send Operating: the choice
 *  would already be selected. */
export function bizShownBucket(
  t: { bucket: string | null; effective_bucket?: string | null },
): string {
  return t.effective_bucket ?? t.bucket ?? "";
}

/** Whether a Books row still needs a Schedule C line: a cost given an
 *  operating bucket and no line. The server keeps such a row in review,
 *  and the Books tab is where it was bucketed, so the line is offered
 *  there too. Start-up and organizational costs are capitalized, not
 *  filed on a line, and a row with no class at all is the review
 *  queue's. */
export function bizNeedsLine(
  t: { amount: number; bucket: string | null; sched_c_line: string | null },
): boolean {
  return t.amount > 0 && t.bucket === "operating" && !t.sched_c_line;
}

/** A typed number for a field that is not money (a tax rate, a floor
 *  area, an ownership share, miles). Blank is null, the field cleared;
 *  "1,200" reads as 1200. Anything else that is not a plain number
 *  ("22,5", "22%") is undefined, refused: sent as Number() it would be
 *  NaN, which JSON writes as null, and the server takes null as "clear"
 *  and wipes the stored value. */
export function numberDraft(v: string): number | null | undefined {
  const t = (v ?? "").trim();
  if (!t) return null;
  const plain = /^\d{1,3}(,\d{3})+(\.\d+)?$/.test(t) ? t.replace(/,/g, "") : t;
  if (!/^-?(\d+\.?\d*|\.\d+)$/.test(plain)) return undefined;
  const n = Number(plain);
  return Number.isFinite(n) ? n : undefined;
}

/** The number fields a save sends: only those whose text differs from
 *  what the form was loaded with, so saving one field never writes back
 *  a stale copy of another that changed elsewhere since. Throws, naming
 *  the field, when its text is not a number (see numberDraft). */
export function numberFieldsPatch(
  fields: Record<string, { draft: string; loaded: string; label: string }>,
): Record<string, number | null> {
  const out: Record<string, number | null> = {};
  for (const [key, f] of Object.entries(fields)) {
    if (f.draft.trim() === f.loaded.trim()) continue;
    const n = numberDraft(f.draft);
    if (n === undefined) throw new Error(`The ${f.label} is not a number`);
    out[key] = n;
  }
  return out;
}

/** Whether a refused member edit or removal means the server has no such
 *  door (an older server): an unknown path answers the framework's bare
 *  "Not Found", a known path asked with another method 405. A 404 that
 *  names the member is the member already gone, not a missing door. */
export function bizMemberDoorMissing(
  status: number | null | undefined, detail: string | null | undefined,
): boolean {
  return status === 405
    || (status === 404 && (detail ?? "").trim() === "Not Found");
}
