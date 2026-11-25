// ONE budget language across Today / Budget / Month / Week.
//
// The setup wizard teaches a plan — bills · budget categories ·
// unallocated · savings — so tracking speaks the same words in the same
// order. The rows are the server's (todayview.map_rows, sent as
// `plan_rows` by Today and both lenses); nothing here builds one.
import { Link } from "react-router";
import { RowAddsUp } from "./BillsTable";
import { money, type PlanTotal, type RowStatus, type ServerPlanRow }
  from "../api/client";

export interface PlanRow {
  label: string; actual: number; expected: number; budget: number;
  pending?: number; caption?: string; judged: boolean;
  // where the row's transactions are — the label opens them
  to?: string;
  // header override for the actual side — "—" when nothing is measurable
  // (plan-only savings), so it can't read as measured-and-zero
  actualLabel?: string;
  // what the row says beside its figures (todayview.row_status): over /
  // over pace so far / left / still due; null says nothing
  status?: RowStatus | null;
  // the Bills row's parts and the table they open (bills_breakdown), the
  // Envelopes row's pools (envelope_breakdown); absent on every other row
  breakdown?: ServerPlanRow["breakdown"];
}

/** The plan rows the server built (Today's payload and the Month and Year
 *  lens replies, `plan_rows`), in the shape the maps draw — nothing is
 *  computed here. */
export function sentPlanRows(rows: readonly ServerPlanRow[]): PlanRow[] {
  return rows.map((r) => ({
    label: r.label, judged: r.judged, actual: r.actual,
    expected: r.expected, budget: r.budget,
    pending: r.pending || undefined, to: r.to ?? undefined,
    actualLabel: r.actual_label ?? undefined,
    caption: r.caption ?? undefined,
    status: r.status,
    breakdown: r.breakdown,
  }));
}

/** The "Variable spending" row the server sums over the judged rows
 *  (todayview.plan_total), with its status. */
export function sentPlanTotal(t: PlanTotal): PlanRow {
  return { label: t.label, judged: t.judged, actual: t.actual,
           expected: t.expected, budget: t.budget, status: t.status };
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
  // a plan-only row the server words (the Excess cash row's caption) is
  // those words alone — a "— / $X" over an empty bar left a raw dash to
  // read. A plan-only row without words (a Savings plan with nothing to
  // measure) keeps its figure below, as Today's footer and the email do.
  if (row.actualLabel === "—" && row.caption)
    return (
      <div style={{ margin: ".6rem 0", fontSize: 13,
                    ...(indent ? { marginLeft: `${1.5 * indent}rem` } : {}) }}>
        <span>{row.label}</span>
        {row.caption && <div className="sub" style={{ fontSize: 11 }}>
          {row.caption}</div>}
      </div>);
  return (
    <div style={{ margin: ".6rem 0",
                  ...(indent ? { marginLeft: `${1.5 * indent}rem` } : {}) }}>
      <div style={{ display: "flex", justifyContent: "space-between",
                    fontSize: 13 }}>
        <span>{row.label}</span>
        <span className="sub" style={{ textAlign: "right" }}>
          {row.actualLabel ?? money(row.actual)} / {unbudgeted ? "—" : money(row.budget)}
          {/* the Bills and Envelopes rows' words, as the map prints them
              beside their figures; no other row's here */}
          {(row.label === "Bills" || row.label === "Envelopes")
            && row.status && (
            <span style={{ marginLeft: ".45rem" }}>{row.status.text}</span>)}
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
      {/* the Bills row opens how its figures add up, the Envelopes row
          its pools, when the server sends them */}
      {row.breakdown?.table && <RowAddsUp breakdown={row.breakdown} />}
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
