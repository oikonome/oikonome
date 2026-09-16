// ONE budget language across Today / Budget / Month / Week.
//
// The setup wizard teaches a plan — bills · budget categories ·
// unallocated · savings — so tracking speaks the same words in the same
// order. The verdict math is untouched: this is a display grouping over
// the payload the engine already returns (buckets.*.children carry the
// carve-outs; display_actual/display_expected are the parent
// after those carve-outs).
import { Link } from "react-router";
import { money } from "../api/client";

export interface PlanBucket {
  actual: number; expected: number; month_budget: number;
  children?: { name: string; actual: number; expected: number;
               month_budget: number }[];
  display_actual?: number; display_expected?: number;
  remaining_budget?: number;
}
export interface PlanRow {
  label: string; actual: number; expected: number; budget: number;
  pending?: number; caption?: string; judged: boolean;
  // where the row's transactions are — the label opens them
  to?: string;
  // header override for the actual side — "—" when nothing is measurable
  // (plan-only savings), so it can't read as measured-and-zero
  actualLabel?: string;
}

/** Plan rows in the wizard's order: categories (Food first, then the
 *  carve-outs), Unallocated, Bills, Savings. `scale` converts monthly
 *  plan numbers to the timeframe (week lens passes 1/4.3). */
export function planRows(
  buckets: Record<string, PlanBucket>,
  opts: { scale?: number; fixedActual?: number; fixedBudget?: number;
          fixedPending?: number; fixedCount?: number;
          savings?: { plan: number; saved: number | null } | null;
          excess?: number | null;
          // the month the rows cover; the ledger link opens that month
          // (asOf: the day the figures were read on, when not today)
          period?: { y: number; m: number; asOf?: string } | null } = {},
): PlanRow[] {
  const k = opts.scale ?? 1;
  const rows: PlanRow[] = [];
  // A plan row's number is a BUCKET of the month's verdict, not a category
  // filter: Food also counts the food-override rows of merchants filed
  // elsewhere (a warehouse club, a marketplace), a custom bucket is a plan label the ledger
  // has never stored on a transaction, and "Everything else" is whatever
  // the carve-outs left. So the label opens the ledger BY BUCKET, which
  // lists exactly the rows the verdict counted. A bucket is always a
  // month's bucket, so a map drawn without one (the yearly map, the budget
  // page's plan) has no addressable filter and its labels stay plain text.
  const ledger = (bucket: string) =>
    opts.period
      ? `/transactions?bucket=${encodeURIComponent(bucket)}`
        + `&y=${opts.period.y}&m=${opts.period.m}`
        + (opts.period.asOf ? `&as_of=${opts.period.asOf}` : "")
      : undefined;
  const food = buckets.food;
  const other = buckets.other;
  if (food) {
    const kids = food.children ?? [];
    rows.push({
      label: "Food", judged: true, to: ledger("food"),
      actual: kids.length ? (food.display_actual ?? food.actual) : food.actual,
      expected: kids.length ? (food.display_expected ?? food.expected)
                            : food.expected,
      budget: kids.length ? (food.remaining_budget ?? food.month_budget)
                          : food.month_budget,
    });
    for (const c of kids)
      rows.push({ label: c.name, actual: c.actual, expected: c.expected,
                  budget: c.month_budget, judged: true, to: ledger(c.name) });
  }
  if (other) {
    for (const c of other.children ?? [])
      rows.push({ label: c.name, actual: c.actual, expected: c.expected,
                  budget: c.month_budget, judged: true, to: ledger(c.name) });
    const kids = other.children ?? [];
    rows.push({
      label: "Everything else", judged: true, to: ledger("other"),
      caption: kids.length ? "unallocated — spending outside the categories"
                           : undefined,
      actual: kids.length ? (other.display_actual ?? other.actual)
                          : other.actual,
      expected: kids.length ? (other.display_expected ?? other.expected)
                            : other.expected,
      budget: kids.length ? (other.remaining_budget ?? other.month_budget)
                          : other.month_budget,
    });
  }
  // bills + savings are IN the plan but never in the verdict
  if (opts.fixedBudget !== undefined || opts.fixedActual !== undefined) {
    const posted = opts.fixedActual ?? 0;
    rows.push({
      label: "Bills", judged: false, actual: posted,
      expected: buckets.fixed?.expected ?? 0,
      budget: opts.fixedBudget ?? buckets.fixed?.month_budget ?? 0,
      pending: opts.fixedPending,
      caption: `posted ${money(posted)}`
        + (opts.fixedPending ? ` · awaiting ${money(opts.fixedPending)}` : "")
        + " · not counted in the verdict",
    });
  }
  if (opts.savings
      && (opts.savings.plan > 0 || (opts.savings.saved ?? 0) > 0)) {
    // saved === null is a plan-only goal (no destination account) —
    // there are no transfers to measure, so the bar carries the plan alone
    const planOnly = opts.savings.saved === null;
    rows.push({
      label: "Savings / Investment", judged: false,
      actual: opts.savings.saved ?? 0, expected: opts.savings.plan * k,
      budget: opts.savings.plan * k,
      actualLabel: planOnly ? "—" : undefined,
      caption: planOnly
        ? "plan only — set a destination account on the Budget page to track transfers · not counted in the verdict"
        : "transferred vs plan · not counted in the verdict",
    });
  }
  // the plan's residual — income the plan deliberately leaves
  // unassigned. Nothing to track (it just stays in checking), so the
  // actual side is "—" like a plan-only savings row.
  if (opts.excess != null && opts.excess > 0.005) {
    rows.push({
      label: "Excess cash", judged: false,
      actual: 0, actualLabel: "—",
      expected: opts.excess * k, budget: opts.excess * k,
      caption: "unplanned — stays in checking · not counted in the verdict",
    });
  }
  return rows;
}

/** One bullet bar. Green/red vs the pace post tick; amber for the
 *  scheduled-but-unposted segment. a category with spending but NO budget
 *  is amber "no budget set" — red must mean over budget, not unbudgeted. */
function PlanBar({ row, indent = 0 }: { row: PlanRow; indent?: number }) {
  const unbudgeted = row.budget <= 0.005 && row.actual > 0.005;
  const mb = row.budget || 1;
  const fillw = Math.min((100 * row.actual) / mb, 100);
  const pendw = row.pending
    ? Math.min((100 * row.pending) / mb, 100 - fillw) : 0;
  const tick = Math.min((100 * row.expected) / mb, 100);
  // same "over" semantics as the MoneyMap — judged (verdict) rows go
  // red past the pace tick; unjudged rows (Bills / Savings) only past the
  // BUDGET. A bill posting ahead of pace is lumpy, not an overrun.
  const isover = row.judged ? row.actual > row.expected + 0.5
                            : row.actual > row.budget + 0.5;
  return (
    <div style={{ margin: ".6rem 0",
                  ...(indent ? { marginLeft: `${1.5 * indent}rem` } : {}) }}>
      <div style={{ display: "flex", justifyContent: "space-between",
                    fontSize: 13 }}>
        <span>{row.label}</span>
        <span className="sub">
          {row.actualLabel ?? money(row.actual)} / {unbudgeted ? "—" : money(row.budget)}
        </span>
      </div>
      {unbudgeted ? (
        <div className="bartrack" style={{ marginTop: 3, display: "flex" }}>
          <div style={{ width: "100%", background: "var(--amber)",
                        opacity: .45 }} />
        </div>
      ) : (
        <div style={{ position: "relative", marginTop: 3 }}>
          <div className="bartrack" style={{ display: "flex" }}>
            {fillw > 0.2 && (
              <div style={{ width: `${fillw}%`,
                            background: isover ? "var(--red)"
                                               : "var(--green)" }} />
            )}
            {pendw > 0.2 && <div style={{ width: `${pendw}%`,
                                          background: "var(--amber)" }} />}
          </div>
          <div className="bartick" style={{ left: `${tick}%` }} />
        </div>
      )}
      {unbudgeted ? (
        <div className="sub" style={{ fontSize: 11, marginTop: 1,
              color: "var(--amber)" }}>
          no budget set — <Link to="/budget">set one</Link> so this counts
          toward the verdict
        </div>
      ) : row.caption ? (
        <div className="sub" style={{ fontSize: 11, marginTop: 1 }}>
          {row.caption}</div>
      ) : null}
    </div>
  );
}

/** The plan, tracked: a total bar over the per-category rows. */
export default function PlanBars({ rows, total }: {
  rows: PlanRow[];
  total?: { label: string; actual: number; expected: number; budget: number };
}) {
  const judged = rows.filter((r) => r.judged);
  const unjudged = rows.filter((r) => !r.judged);
  return (
    <>
      {total && (
        <PlanBar row={{ ...total, judged: true }} />
      )}
      {judged.map((r) => (
        <PlanBar key={r.label} row={r} indent={total ? 1 : 0} />
      ))}
      {unjudged.map((r) => <PlanBar key={r.label} row={r} />)}
    </>
  );
}
