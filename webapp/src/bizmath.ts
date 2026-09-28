// Business-page arithmetic and wizard routing, kept free of React so the
// same rules can be exercised by a plain-node test. The phone carries an
// identical copy in mobile/src/lib/pure.ts, and the mobile pure-logic suite
// runs both copies over the same cases — the two clients must never show a
// business different numbers or open its wizard on a different business.

// Banker's rounding, the same as api/client.ts's pyround. Copied rather than
// imported so this module stays loadable by plain node (the mobile pure-logic
// suite imports it directly) and free of the React client's dependency graph.
const pyround = (v: number) => {
  const f = Math.floor(v), diff = v - f;
  if (diff > 0.5) return f + 1;
  if (diff < 0.5) return f;
  return f % 2 === 0 ? f : f + 1;
};

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

/** A business transaction as the P&L reads it (books.entity_transactions). */
export interface BizProfitRow {
  date: string | null; amount: number;
  /** the amount net of partial reimbursement links, ledger sign kept */
  net_amount?: number | null;
  category: string | null; cat_detailed: string | null;
  cat_original?: string | null;
  /** a deposit whose links claim only part of it — still revenue */
  partly_claimed?: boolean | null;
  bucket: string | null;
  /** the bucket the server's P&L counted this row in: a refund takes the
   *  bucket of the cost it reverses, which only the server can find (the
   *  charge may sit in another period or year). Null for money in that is
   *  not a refund; absent from an older server. */
  effective_bucket?: string | null;
}

/** Money IN still carrying one of these spending categories is a merchant
 *  giving money back — a refund that reverses a cost in its own bucket, not
 *  revenue. The server's list (books._REFUND_CATEGORIES); the mobile
 *  pure-logic suite checks the two clients' copies against it. */
export const BIZ_REFUND_CATEGORIES: ReadonlySet<string> = new Set([
  "BANK_FEES", "ENTERTAINMENT", "FOOD_AND_DRINK", "GENERAL_MERCHANDISE",
  "GENERAL_SERVICES", "GOVERNMENT_AND_NON_PROFIT", "HOME_IMPROVEMENT",
  "MEDICAL", "PERSONAL_CARE", "RENT_AND_UTILITIES", "TRANSPORTATION",
  "TRAVEL", "PROFESSIONAL_SERVICES", "INSURANCE",
]);

/** Operating profit over the rows whose date starts with `period`
 *  ("2026-08" for a month), by the server's P&L rules, so a period figure
 *  shown under the year's profit is the same arithmetic.
 *
 *  - Amounts are NET of reimbursement links: a charge paid back counts only
 *    what was not repaid, a deposit only the part no charge claimed. Gross
 *    figures would count one client check that repays two charges as both
 *    revenue and a cost.
 *  - Money in is revenue unless it is a transfer or a card payment, and
 *    money whose ORIGINAL category was a transfer stays capital unless it
 *    was made INCOME outright — except a partly-claimed deposit, whose
 *    unclaimed part is revenue whatever its category says.
 *  - Money in that still carries a spending category is a refund: it
 *    reverses a cost in its own bucket, so a start-up or organizational
 *    refund leaves the period untouched (its cost was capitalized) and an
 *    operating refund lowers operating cost. The row's `effective_bucket`
 *    names that bucket when the server sent it — a refund dated after the
 *    business opened can reverse a start-up purchase. Counted as revenue, a
 *    returned pre-opening purchase would show as profit.
 *  - Money out is an operating cost only in the operating bucket;
 *    organizational and start-up costs are capitalized, and an unbucketed
 *    cost dated before the business opened is a start-up cost. */
export function bizPeriodProfit(
  rows: readonly BizProfitRow[], businessStart: string | null | undefined,
  period: string,
): number {
  let revenue = 0;
  let operating = 0;
  for (const t of rows) {
    if (!t.date || !t.date.startsWith(period)) continue;
    const net = t.net_amount ?? t.amount;
    const cat = (t.category || "").toUpperCase();
    const card = (t.cat_detailed || "") === "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT";
    // the server's own bucket first, so a refund nets against the cost it
    // reverses rather than being re-bucketed by its own date
    const bucket = t.effective_bucket ?? (t.bucket
      || (businessStart && t.date < businessStart
        ? "startup_195" : "operating"));
    const capitalized = bucket === "organizational"
      || bucket === "startup_195";
    if (t.amount < 0) {
      if (t.partly_claimed) { revenue += -net; continue; }
      const wasTransfer = (t.cat_original || "").toUpperCase()
        .includes("TRANSFER");
      if (cat.includes("TRANSFER") || card
          || (wasTransfer && cat !== "INCOME")) continue;
      if (BIZ_REFUND_CATEGORIES.has(cat)) {
        // net is negative: the refund lowers the cost it reverses
        if (!capitalized) operating += net;
      } else revenue += -net;
      continue;
    }
    if (cat === "TRANSFER_OUT" || cat === "TRANSFER_IN" || card) continue;
    if (capitalized) continue;
    if (net > 0) operating += net;
  }
  return pyround((revenue - operating) * 100) / 100;
}
