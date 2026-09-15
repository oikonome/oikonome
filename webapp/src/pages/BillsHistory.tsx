// Per-payee recurring history: bill status header, ledger fit ("edit with
// these values", hint dismiss/restore), add-as-bill form, payee-scoped
// proposals, lifetime totals, monthly table, 24-month transactions with
// matcher ✓.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, errText, mmdd, money as money$, type BillsHistoryData, catLabel } from "../api/client";
import { patchQuery, removeFromList } from "../api/cache";
import MerchantAvatar from "../components/MerchantAvatar";
import TxnTable from "../components/TxnTable";
import { SIMPLE_CADENCES } from "./Bills";
import { canEdit } from "../role";
const money = (v: number | null | undefined) => money$(v, true);  // cents on transaction/recurring surfaces


const hpill = (h: string) =>
  h === "ok" ? <span className="pill g">matching</span>
  : h === "drifting" ? <span className="pill a">drifting</span>
  : h === "stale" ? <span className="pill r">stale</span>
  : h === "mismatch" ? <span className="pill a">amounts differ</span>
  // misdated is named here exactly as the Bills page names it — one bill
  // must not tell two different stories
  : h === "misdated" ? <span className="pill a">misdated</span>
  : <span className="pill m">off-ledger</span>;

export default function RecurringHistory() {
  const [sp] = useSearchParams();
  const payee = sp.get("payee") ?? "";
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["bills-history", payee],
    queryFn: () => api.billsHistory(payee),
    enabled: !!payee,
  });
  // hint dismiss/restore, proposal act and add-as-bill are editing
  // writes — viewers keep the whole history read view
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  const refetch = () => {
    qc.invalidateQueries({ queryKey: ["bills-history", payee] });
    qc.invalidateQueries({ queryKey: ["bills"] });
  };
  // the merchant-wide recategorize (and its undo) rewrites ledger rows, so
  // beyond this page it moves the Merchants detail panel (category + rule),
  // the transactions list and the verdict
  const refetchAfterRecat = () => {
    refetch();
    qc.invalidateQueries({ queryKey: ["merchant-detail"] });
    qc.invalidateQueries({ queryKey: ["txns"] });
    qc.invalidateQueries({ queryKey: ["today"] });
  };
  const hint = useMutation({
    mutationFn: (action: "dismiss" | "restore") => api.billsHint(payee, action),
    onSuccess: (r) => {
      // the flag is the whole change: write it into the 24-month payload
      // rather than refetching it, and refresh only the Bills list (where
      // the 💡 hint shows or hides)
      patchQuery<BillsHistoryData>(qc, ["bills-history", payee],
                                   { hint_dismissed: r.dismissed });
      qc.invalidateQueries({ queryKey: ["bills"] });
    },
  });
  const proposal = useMutation({
    mutationFn: ({ pid, action }: { pid: string; action: "approve" | "reject" }) =>
      api.billsProposal(pid, action),
    onSuccess: (_r, v) => {
      // the decided proposal leaves the list now — the server has already
      // committed it, and waiting on the 24-month payload to re-prove that
      // left the row sitting there
      removeFromList<BillsHistoryData, BillsHistoryData["proposals"][number]>(
        qc, ["bills-history", payee], "proposals", (p) => p.id === v.pid);
      qc.invalidateQueries({ queryKey: ["bills"] });
      // rejecting writes no bill; approving changes one, which moves this
      // page's header, the verdict and the calendar
      if (v.action === "approve") {
        qc.invalidateQueries({ queryKey: ["bills-history", payee] });
        qc.invalidateQueries({ queryKey: ["today"] });
        qc.invalidateQueries({ queryKey: ["calendar"] });
      }
      // the budgets prefill reads pending income proposals either way
      qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    },
  });
  // only the proposal being decided is busy, not every row's buttons
  const deciding = (pid: string) =>
    proposal.isPending && proposal.variables?.pid === pid;

  if (!payee) return <div className="note bad">No payee given.</div>;
  if (q.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (q.isError) return <div className="note bad">Couldn't load: {String(q.error)}</div>;
  const d: BillsHistoryData = q.data;
  const bill = d.bill;
  const fit = d.fit;
  const cadLabel = (v: string) => d.cadences.find(([cv]) => cv === v)?.[1] ?? v;
  const showDismiss = d.health &&
    (["mismatch", "stale", "misdated"].includes(d.health.health) || d.health.fit_diverges);

  return (
    <>
      <h1>History — {d.payee}</h1>
      <div className="card" style={{ marginTop: 0 }}>
        <h2><MerchantAvatar name={d.payee} logo={d.logo} size={26}
                            style={{ marginRight: 8 }} />{d.payee}{" "}
          {d.health && <>{hpill(d.health.health)}{" "}</>}
          {bill && <><span className="pill m">{bill.bill_type}</span>{" "}
            {bill.income && <span className="pill m">income</span>}</>}
        </h2>
        <div className="mut" style={{ fontSize: 13 }}>
          {bill ? (
            <>Configured: {money(bill.amount)} {cadLabel(bill.cadence)}
              {bill.due_on && <> · next due {mmdd(bill.due_on)}</>}
              {" "}· source {bill.source}</>
          ) : d.covered_by ? (
            <>Already tracked by bill{" "}
              <Link to={`/bills/history?payee=${encodeURIComponent(d.covered_by)}`}>
                {d.covered_by}</Link>.</>
          ) : (
            <>Not an active bill.</>
          )}
          {d.health?.last_match && (
            <> · last matched {mmdd(d.health.last_match)}
              {" "}({d.health.matched} matches)</>
          )}
        </div>
        {fit && (
          <div className="fitline" style={{ marginTop: ".6rem", fontSize: 13 }}>
            <strong>Ledger says:</strong> {fit.label}
            {bill && mayEdit && (
              <>
                <Link className="btn" style={{ marginLeft: ".6rem" }}
                      to={`/bills?edit=${encodeURIComponent(d.payee)}` +
                          `&pamt=${fit.amount}&pcad=${encodeURIComponent(fit.cadence)}` +
                          `&pdue=${fit.next_due || ""}`}>
                  edit with these values</Link>
                {d.hint_dismissed ? (
                  <>
                    <span className="pill m" style={{ marginLeft: ".6rem" }}
                          title="the 💡 hint is hidden on the Bills page until the ledger fit changes">
                      hint dismissed</span>{" "}
                    <button style={{ marginLeft: ".3rem" }} disabled={hint.isPending}
                            onClick={() => hint.mutate("restore")}>restore hint</button>
                  </>
                ) : showDismiss ? (
                  <button style={{ marginLeft: ".3rem" }} disabled={hint.isPending}
                          title="hide the 💡 hint on the Bills page — it returns only if the ledger fit changes"
                          onClick={() => hint.mutate("dismiss")}>dismiss hint</button>
                ) : null}
              </>
            )}
          </div>
        )}
        {!bill && !d.covered_by && mayEdit && <AddAsBill d={d} onDone={refetch} />}
      </div>

      {mayEdit && <MerchantCategory payee={d.payee} onDone={refetchAfterRecat} />}

      {d.proposals.length > 0 && (
        <div className="card">
          <h2>Pending proposals</h2>
          {d.proposals.map((p) => (
            <div key={p.id} style={{ display: "flex", gap: ".7rem", alignItems: "center",
                                     flexWrap: "wrap", padding: ".3rem 0" }}>
              <span className={"pill " + (p.kind === "remove" ? "r" : p.kind === "add" ? "g" : "a")}>
                {catLabel(p.kind)}</span>
              {p.bill_type === "envelope" && <span className="pill m">envelope</span>}
              <strong>{money(p.amount)}</strong>
              <span className="mut" style={{ fontSize: 13 }}>{p.summary}</span>
              {mayEdit && (<>
              <button className="pri" disabled={deciding(p.id)}
                      onClick={() => proposal.mutate({ pid: p.id, action: "approve" })}>
                approve</button>
              <button disabled={deciding(p.id)}
                      onClick={() => proposal.mutate({ pid: p.id, action: "reject" })}>
                reject</button>
              </>)}
            </div>
          ))}
        </div>
      )}

      {d.lifetime.count > 0 && (
        <div className="card">
          <h2>Lifetime total</h2>
          <div style={{ display: "flex", gap: "2rem", flexWrap: "wrap", alignItems: "baseline" }}>
            <div>
              <span style={{ fontSize: "1.6rem", fontWeight: 700,
                             color: d.inflow ? "var(--green)" : undefined }}>
                {money(d.lifetime.total)}</span>
              <span className="mut"> {d.inflow ? "received" : "spent"} all-time</span>
            </div>
            <div className="mut">{d.lifetime.count} charge{d.lifetime.count === 1 ? "" : "s"}</div>
            {d.lifetime.active_months > 0 && (
              <div>
                <span style={{ fontWeight: 600 }}>
                  {money(d.lifetime.avg_monthly_active)}</span>
                <span className="mut"> avg / mo (active) ·{" "}
                  {d.lifetime.active_months} active month
                  {d.lifetime.active_months === 1 ? "" : "s"}</span>
              </div>
            )}
            {d.lifetime.first && (
              <div className="mut">{mmdd(d.lifetime.first)} – {mmdd(d.lifetime.last)}</div>
            )}
          </div>
        </div>
      )}

      <div className="card">
        <h2>Transactions{" "}
          <span className="mut" style={{ fontSize: 13, fontWeight: 400 }}>
            (24 months)</span></h2>
        {/* same renderer as the Transactions page — full recategorize /
            reimb / biz / receipt affordances, no drift */}
        {d.txns.length > 0
          ? <TxnTable rows={d.txns} />
          : <p className="mut">No ledger transactions match this payee.</p>}
      </div>

      {/* monthly totals BELOW the ledger */}
      {d.monthly.length > 0 && (
        <div className="card">
          <h2>Monthly totals</h2>
          <table style={{ tableLayout: "fixed", maxWidth: "26rem" }}>
            <colgroup><col style={{ width: "8rem" }} /><col style={{ width: "8rem" }} /><col /></colgroup>
            <thead><tr><th>Month</th><th className="num">Total</th>
              <th className="num">Charges</th></tr></thead>
            <tbody>
              {d.monthly.map((m) => (
                <tr key={m.month}>
                  <td className="mut">{mmdd(m.month + "-01")}</td>
                  <td className="num">{money(m.total)}</td>
                  <td className="num">{m.count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p style={{ marginTop: "1rem" }}>
        <Link className="btn" to="/bills">← back to Bills</Link></p>
    </>
  );
}

function AddAsBill({ d, onDone }: { d: BillsHistoryData; onDone: () => void }) {
  const [amount, setAmount] = useState(d.fit ? d.fit.amount.toFixed(2) : "");
  const [cadence, setCadence] = useState(d.fit?.cadence ?? "MONTHLY:1");
  const [nextDue, setNextDue] = useState(d.fit?.next_due ?? "");
  const [err, setErr] = useState<string | null>(null);
  // "Custom interval…" → any FREQ:interval (every X months / years) for
  // unusual bills. The engine schedules + budgets any
  // interval already, so this just widens the picker.
  const [customN, setCustomN] = useState("2");
  const [customUnit, setCustomUnit] = useState("YEARLY");
  const effCadence = cadence === "CUSTOM"
    ? `${customUnit}:${Math.max(1, parseInt(customN, 10) || 1)}`
    : cadence;
  // explicit user action, distinct from the silent defaults above: fill
  // the form from the ledger fit and SAY what the ledger saw (fit.label),
  // so choosing e.g. the envelope suggestion is an informed click. The
  // ENVELOPE:1 cadence value alone carries the bill type — the server's
  // /bills/save maps freq ENVELOPE → bill_type "envelope".
  const [suggested, setSuggested] = useState(false);
  const suggest = () => {
    if (!d.fit) return;
    setAmount(d.fit.amount.toFixed(2));
    setCadence(d.fit.cadence);
    setNextDue(d.fit.next_due ?? "");
    setSuggested(true);
  };
  const add = useMutation({
    mutationFn: () => api.billsSave({
      payee: d.payee, merchant: d.payee, amount,
      cadence: effCadence, next_due: nextDue,
    }),
    onSuccess: onDone,
    onError: (e) => setErr(errText(e)),
  });
  return (
    <form style={{ marginTop: ".7rem", display: "flex", gap: ".5rem",
                   alignItems: "center", flexWrap: "wrap", fontSize: 13 }}
          onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
      <strong>Add as recurring bill:</strong>
      $<input type="number" step="0.01" required style={{ width: "7rem" }}
              value={amount} onChange={(e) => setAmount(e.target.value)} />
      <select value={cadence} onChange={(e) => setCadence(e.target.value)}>
        {SIMPLE_CADENCES.map(([v, label]) => (
          <option key={v} value={v}>{label}</option>
        ))}
        {/* a ledger suggestion may be an unusual cadence not in the short
            list — keep it selectable/visible rather than silently mis-showing */}
        {cadence !== "CUSTOM" && !SIMPLE_CADENCES.some(([v]) => v === cadence) && (
          <option value={cadence}>
            {d.cadences.find(([v]) => v === cadence)?.[1] ?? cadence}</option>
        )}
        <option value="CUSTOM">Custom interval…</option>
      </select>
      {cadence === "CUSTOM" && (
        <span>every <input type="number" min="1" style={{ width: "3.5rem" }}
               value={customN} onChange={(e) => setCustomN(e.target.value)} />
          <select value={customUnit}
                  onChange={(e) => setCustomUnit(e.target.value)}>
            <option value="MONTHLY">month(s)</option>
            <option value="YEARLY">year(s)</option>
          </select></span>
      )}
      <input type="date" value={nextDue} onChange={(e) => setNextDue(e.target.value)} />
      <button className="pri" disabled={add.isPending}>add</button>
      {d.fit ? (
        <button type="button" onClick={suggest}
                title={d.fit.label}>✨ suggest from history</button>
      ) : (
        // the fit needs ≥3 charges and a provable cycle or a monthly
        // envelope pattern — be honest about why there's no button
        <span className="mut"
              title="a suggestion needs at least 3 charges with a steady cycle, or activity in each of the last 3 months">
          ✨ no suggestion — not enough charge history yet</span>
      )}
      {err && <span className="neg">{err}</span>}
      {suggested && d.fit && (
        <span className="mut" style={{ flexBasis: "100%" }}>
          Ledger says: {d.fit.label}
          {d.fit.kind === "envelope" &&
            " — saves as an envelope (monthly pool) bill"}
        </span>
      )}
    </form>
  );
}

// bulk category override for the whole (canonical) merchant.
// Owner-only. preview → confirm-with-count → apply → undo. The count is the
// TRUE merchant-wide scope, so the user consents before the write.
function MerchantCategory({ payee, onDone }: { payee: string; onDone: () => void }) {
  const [cat, setCat] = useState("");
  const [confirm, setConfirm] = useState<
    Awaited<ReturnType<typeof api.merchantCategoryPreview>> | null>(null);
  const [undo, setUndo] = useState<{ count: number; merchant: string; undo: unknown } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // the demo instance refuses merchant-wide category writes (its login is
  // shared) — disabled-in-place beats a 403 after the picker, the same
  // gate Merchants applies (reads the same cached /api/me query)
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const demo = !!me.data?.demo;
  // standard Plaid SPEND primaries for the picker; anything this tenant
  // already uses that ISN'T a standard category (custom categories from the
  // row picker) stays reachable so the select loses nothing the free-text
  // input had. Flow categories (transfers, income, loan payments) are NOT
  // offered: a merchant rule cannot make a merchant a transfer — the server
  // refuses that write — so listing them only produced an error after the
  // confirm.
  const cats = useQuery({ queryKey: ["cats"], queryFn: api.categories });
  const std = new Set([...(cats.data?.plaid_spend ?? []),
                       ...(cats.data?.plaid_flow ?? [])]);
  const custom = (cats.data?.categories ?? []).filter((c) => !std.has(c));

  const preview = async () => {
    const c = cat.trim();
    if (!c) return;
    setErr(null); setBusy(true);
    try {
      setConfirm(await api.merchantCategoryPreview(payee, c));
    }
    catch (e) { setErr(errText(e)); }
    finally { setBusy(false); }
  };
  const apply = async () => {
    setErr(null); setBusy(true);
    try {
      const r = await api.merchantCategorySet(payee, cat.trim());
      setUndo({ count: r.count, merchant: r.merchant, undo: r.undo });
      setConfirm(null); setCat(""); onDone();
    } catch (e) { setErr(errText(e)); }
    finally { setBusy(false); }
  };
  const doUndo = async () => {
    if (!undo) return;
    setBusy(true);
    try { await api.merchantCategoryUndo(undo.undo); setUndo(null); onDone(); }
    catch (e) { setErr(errText(e)); }
    finally { setBusy(false); }
  };

  return (
    <div className="card">
      <h2>Categorize this merchant</h2>
      <p className="mut" style={{ marginTop: 0 }}>
        Set one category for every past and future transaction of this merchant.
      </p>
      {demo && (
        <div className="sub" style={{ margin: "0 0 .5rem",
              color: "var(--amber)" }}>
          Not available on the demo instance — synthetic data only.
        </div>
      )}
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap", alignItems: "center" }}>
        <select value={cat} onChange={(e) => setCat(e.target.value)}
                style={{ minWidth: "16rem" }}>
          <option value="">choose a category…</option>
          <optgroup label="Spending">
            {cats.data?.plaid_spend.map((c) => (
              <option key={c} value={c}>{catLabel(c)}</option>
            ))}
          </optgroup>
          {custom.length > 0 && (
            <optgroup label="Your own categories">
              {custom.map((c) => (
                <option key={c} value={c}>{catLabel(c)}</option>
              ))}
            </optgroup>
          )}
        </select>
        <button className="pri" disabled={busy || demo || !cat.trim()}
                onClick={preview}>
          Apply to whole merchant…
        </button>
      </div>
      {confirm && (
        // display:block — .note is a flex row, which would set the
        // headline, the variant list and the buttons side by side and
        // wrap them badly
        <div className="note" style={{ marginTop: ".6rem", display: "block" }}>
          <div style={{ fontSize: 15 }}>
            {confirm.count > 0 ? <>
              Move <b>{confirm.count}</b> past transaction{confirm.count === 1 ? "" : "s"} to{" "}
              <b>{catLabel(cat.trim())}</b>
            </> : <>
              Every matched charge is already <b>{catLabel(cat.trim())}</b>
            </>}
            {" "}— and keep future charges there.
          </div>
          {/* What this write does beyond the obvious: hand-set rows are left
              alone, and bank-labelled transfers DO move (a merchant rule
              outranks the aggregator's transfer label — Venmo-to-a-person
              rows stayed transfers forever otherwise). Staying silent about
              either is what makes "apply to all" look broken. */}
          {!!(confirm.flow_rows || confirm.skipped_override) && (
            <div className="note" style={{ marginTop: ".5rem", display: "block" }}>
              {!!confirm.flow_rows && (
                <div><b>{confirm.flow_rows}</b> of these row
                  {confirm.flow_rows === 1 ? " is" : "s are"} labelled by your
                  bank as a transfer, income or loan payment and <b>will</b> move
                  too — your merchant rule outranks that label (a Venmo or Zelle
                  payment to a person is a transfer to the bank and whatever you
                  say it is to you). They start counting as spending.</div>
              )}
              {!!confirm.skipped_override && (
                <div style={{ marginTop: confirm.flow_rows ? ".3rem" : 0 }}>
                  <b>{confirm.skipped_override}</b> row
                  {confirm.skipped_override === 1 ? " has" : "s have"} a
                  category you set by hand and will be left alone.</div>
              )}
            </div>
          )}
          {(confirm.variants?.length ?? 0) > 1 && (
            <div className="mut" style={{ fontSize: 13, marginTop: ".35rem" }}>
              Covers every name this page matches:{" "}
              {confirm.variants!.slice(0, 6).join(" · ")}
              {confirm.variants!.length > 6 &&
                ` · +${confirm.variants!.length - 6} more`}
            </div>
          )}
          <div style={{ marginTop: ".6rem", display: "flex", gap: ".5rem" }}>
            <button className="pri" disabled={busy} onClick={apply}>Yes, recategorize</button>
            <button disabled={busy} onClick={() => setConfirm(null)}>Cancel</button>
          </div>
        </div>
      )}
      {undo && (
        <div className="note" style={{ marginTop: ".6rem" }}>
          Recategorized {undo.count} transaction{undo.count === 1 ? "" : "s"} for {undo.merchant}.
          <button style={{ marginLeft: ".5rem" }} disabled={busy} onClick={doUndo}>Undo</button>
        </div>
      )}
      {err && <div className="note bad" style={{ marginTop: ".5rem" }}>{err}</div>}
    </div>
  );
}
