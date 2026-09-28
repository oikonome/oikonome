// Budget history — the frozen record the lenses judge closed months by.
// Read-only list of each month's snapshot (its variable budgets + how it
// was captured), plus the one deliberate action: backfill, which applies
// the CURRENT budget to closed months that predate snapshots. Backfill is
// opt-in retroactive judgment, so the button spells out what it does and
// the rows it creates stay labeled "backfilled" forever. Frozen months
// are never editable here — rewriting the record is the exact thing
// snapshots exist to prevent.
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errText, money, type BudgetSnapshots } from "../api/client";

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const SHOW = 24;   // rows before the list collapses behind "show all"

export default function BudgetHistoryCard({ mayEdit, onToast }: {
  mayEdit: boolean; onToast: (m: string) => void;
}) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["budget-snapshots"],
                       queryFn: api.budgetSnapshots });
  const [all, setAll] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!q.data) return null;
  const d: BudgetSnapshots = q.data;
  if (!d.months.length && !d.missing) return null;   // nothing to say yet

  const backfill = async () => {
    if (!window.confirm(
      `Apply your current budget to ${d.missing} past month${
        d.missing > 1 ? "s" : ""} that have no frozen budget?\n\n` +
      "Those months will be judged against today's numbers — a verdict " +
      "you're choosing retroactively, and the history will label it " +
      "\"backfilled\". This doesn't touch months already frozen.")) return;
    setBusy(true);
    try {
      const r = await api.budgetBackfill();
      onToast(`Froze your current budget for ${r.backfilled} past month${
        r.backfilled === 1 ? "" : "s"}.`);
      await qc.invalidateQueries({ queryKey: ["budget-snapshots"] });
      qc.invalidateQueries({ queryKey: ["lens-year"] });
      qc.invalidateQueries({ queryKey: ["lens-month"] });
    } catch (e) {
      onToast(`Backfill failed: ${errText(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const rows = all ? d.months : d.months.slice(0, SHOW);
  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Budget history
        <span className="sub" style={{ fontWeight: 400 }}>
          {" "}— each month's budget and bill schedule, frozen as the month
          closed</span></h2>
      {d.months.length > 0 && (
        <table>
          <thead><tr><th>Month</th><th className="num">Food</th>
            <th className="num">Everything else</th>
            <th className="num">Total</th>
            {/* "—" = the snapshot predates schedule freezing; showing
                today's bill load there would be exactly the retroactive
                judgment this card exists to prevent */}
            <th className="num hide-m">Fixed bills</th>
            <th className="hide-m" /></tr></thead>
          <tbody>
            {rows.map((r) => (
              <tr key={`${r.y}-${r.m}`}>
                <td>{MONTHS[r.m - 1]} {r.y}</td>
                <td className="num">{money(r.food_monthly)}</td>
                <td className="num">{money(r.other_monthly)}</td>
                <td className="num"><b>{money(r.variable_budget)}</b></td>
                <td className="num hide-m">
                  {r.bills_monthly !== undefined
                    ? money(Math.abs(r.bills_monthly))
                    : <span className="mut">—</span>}
                </td>
                <td className="hide-m">
                  {r.source === "backfill" && (
                    <span className="pill m"
                          title={`Applied retroactively on ${
                            r.captured_at.slice(0, 10)
                          } — not the budget set while the month ran`}>
                      backfilled</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {d.months.length > SHOW && !all && (
        <button className="ghost" style={{ marginTop: ".4rem" }}
                onClick={() => setAll(true)}>
          show all {d.months.length} months
        </button>
      )}
      {d.missing > 0 && (
        <div className="sub" style={{ marginTop: ".6rem" }}>
          {d.missing} earlier month{d.missing > 1 ? "s have" : " has"} no
          frozen budget — the Year page shows net income − spending for
          {d.missing > 1 ? " them" : " it"}.
          {mayEdit && d.budget_set && (<>
            {" "}
            <button className="ghost" disabled={busy} onClick={backfill}>
              {busy ? "freezing…" : "apply my current budget to them"}
            </button>
          </>)}
        </div>
      )}
    </div>
  );
}
