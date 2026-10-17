// Per-payee recurring history: bill status header, ledger fit ("edit with
// these values", hint dismiss/restore), add-as-bill form, payee-scoped
// proposals, lifetime totals, monthly table, and the transactions over the
// site-wide timeframe picker (All by default) with matcher ✓.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router";
import { api, errText, mmdd, money as money$, type BillsData, type BillsHistoryData,
         catLabel, proposalSuperseded, toast } from "../api/client";
import { invalidateRollups, patchQueries, patchQuery,
         removeFromList } from "../api/cache";
import { RANGES, RangePicker, type Range } from "../components/RangePicker";
import MerchantAvatar from "../components/MerchantAvatar";
import TxnTable from "../components/TxnTable";
import { SIMPLE_CADENCES } from "./Bills";
import { canEdit } from "../role";
import { CUSTOM_UNITS, cadenceChoices, cadenceWords, customCadence, customMax }
  from "../cadence";
const money = (v: number | null | undefined) => money$(v, true);  // cents on transaction/recurring surfaces


const hpill = (h: string, withdrawnOn?: string | null) =>
  h === "ok" ? <span className="pill g">matching</span>
  // named and toned as the Bills page names it: the missed cycle WAS
  // charged, and the bank feed took the charge back — a warning
  : h === "withdrawn" ? <span className="pill a"
        title={`charge withdrawn by the bank feed${withdrawnOn ? ` (${mmdd(withdrawnOn)})` : ""}`}>withdrawn</span>
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
  // the transaction list's window; the lifetime card is lifetime either way
  const [span, setSpan] = useState<Range>("all");
  const histKey = ["bills-history", payee, span];
  const q = useQuery({
    queryKey: histKey,
    queryFn: () => api.billsHistory(payee, span),
    enabled: !!payee,
  });
  // hint dismiss/restore, proposal act and add-as-bill are editing
  // writes — viewers keep the whole history read view
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  // the demo instance refuses the rename door, so the merge is not offered
  const demo = !!me.data?.demo;
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
    invalidateRollups(qc);
  };
  const hint = useMutation({
    mutationFn: (action: "dismiss" | "restore") => api.billsHint(payee, action),
    onSuccess: (r) => {
      // the flag is the whole change: write it into the history payload
      // rather than refetching it, and refresh only the Bills list (where
      // the 💡 hint shows or hides)
      patchQuery<BillsHistoryData>(qc, histKey,
                                   { hint_dismissed: r.dismissed });
      qc.invalidateQueries({ queryKey: ["bills"] });
    },
  });
  const proposal = useMutation({
    mutationFn: ({ pid, action }: { pid: string; action: "approve" | "reject" }) =>
      api.billsProposal(pid, action),
    onSuccess: (r, v) => {
      const gone = proposalSuperseded(r);
      if (gone) toast(`${r.payee} is already tracked — the offer was withdrawn`);
      // the approve changed something the person set (a pin cleared)
      else if (r.note) toast(`Approved: ${r.payee} — ${r.note}`);
      // the decided proposal leaves the list now — the server has already
      // committed it, and waiting on the history payload to re-prove that
      // left the row sitting there
      removeFromList<BillsHistoryData, BillsHistoryData["proposals"][number]>(
        qc, histKey, "proposals", (p) => p.id === v.pid);
      qc.invalidateQueries({ queryKey: ["bills"] });
      // rejecting (or a withdrawn offer) writes no bill; approving changes
      // one, which moves this page's header, the verdict and the calendar
      if (v.action === "approve" && !gone) {
        qc.invalidateQueries({ queryKey: ["bills-history", payee] });
        qc.invalidateQueries({ queryKey: ["today"] });
        qc.invalidateQueries({ queryKey: ["calendar"] });
      }
      // the budgets prefill reads pending income proposals either way
      qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    },
    onError: (e, v) => {
      toast(errText(e));
      // an offer for a bill already tracked is withdrawn by the server as
      // it refuses: the card could neither be approved nor dismissed
      if (proposalSuperseded(e)) {
        removeFromList<BillsHistoryData, BillsHistoryData["proposals"][number]>(
          qc, histKey, "proposals", (p) => p.id === v.pid);
        qc.invalidateQueries({ queryKey: ["bills"] });
        qc.invalidateQueries({ queryKey: histKey });
      }
    },
  });
  // only the proposal being decided is busy, not every row's buttons
  const deciding = (pid: string) =>
    proposal.isPending && proposal.variables?.pid === pid;

  if (!payee) return <div className="note bad">No payee given.</div>;
  if (q.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (q.isLoadingError) return <div className="note bad">Couldn't load: {String(q.error)}</div>;
  const d: BillsHistoryData = q.data;
  const bill = d.bill;
  const fit = d.fit;
  const cadLabel = (v: string) =>
    d.cadences.find(([cv]) => cv === v)?.[1] ?? cadenceWords(v);
  const showDismiss = d.health &&
    (["mismatch", "stale", "misdated"].includes(d.health.health) || d.health.fit_diverges);

  return (
    <>
      <h1>History — {d.payee}</h1>
      <div className="card" style={{ marginTop: 0 }}>
        <h2><MerchantAvatar name={d.payee} logo={d.logo} size={26}
                            style={{ marginRight: 8 }} />{d.payee}{" "}
          {d.health && <>{hpill(d.health.health, d.health.withdrawn_on)}{" "}</>}
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
        {/* an archived bill under this payee is restored, not added
            again — a save here would edit that archived row */}
        {!bill && !d.covered_by && mayEdit && (d.bill_archived
          ? <RestoreArchived payee={d.payee} onDone={refetch} />
          : <AddAsBill d={d} onDone={refetch} />)}
        {/* keyed by payee: landing on the target's page must not carry the
            source's half-typed search or open confirm along with it */}
        {mayEdit && !demo && <MergeInto key={d.payee} payee={d.payee}
                                        inUse={d.merchant_in_use} />}
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
        <h2 style={{ display: "flex", gap: ".6rem", alignItems: "center",
                     flexWrap: "wrap" }}>
          Transactions
          <RangePicker value={span} onChange={setSpan} options={RANGES} />
        </h2>
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

// The automatic resolver sometimes gives a renamed feed string its own
// merchant ("Northwind" arriving later as "Northwind Supply"). This is the
// Merchants page's merge, offered where the duplicate is noticed: the same
// journalled rename, so it shows under Recent changes there and undoes the
// same way. The picker searches the catalog, so the target always exists —
// a typo cannot fork a third merchant.
function MergeInto({ payee, inUse }: { payee: string; inUse?: boolean }) {
  const qc = useQueryClient();
  const nav = useNavigate();
  const [open, setOpen] = useState(false);
  const [pick, setPick] = useState("");
  const [pickQ, setPickQ] = useState("");
  const [target, setTarget] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => {
    const t = window.setTimeout(() => setPickQ(pick.trim()), 250);
    return () => window.clearTimeout(t);
  }, [pick]);
  const cands = useQuery({ queryKey: ["merchants", pickQ, "count"],
                           queryFn: () => api.merchantCatalog(pickQ, "count"),
                           enabled: open && pickQ.length > 0,
                           placeholderData: (prev) => prev });
  const options = (cands.data?.merchants ?? [])
    .filter((m) => m.display !== payee).slice(0, 8);
  // The merge renames what ledger rows display. A bill can carry a name of
  // its own that no row shows, and the rename door has nothing to move for
  // it, so the control only appears for a name the ledger displays. A
  // server that does not say so is asked through the catalog instead.
  const known = useQuery({ queryKey: ["merchants", payee, "count"],
                           queryFn: () => api.merchantCatalog(payee, "count"),
                           enabled: inUse === undefined && !!payee });
  const isMerchant = inUse ?? !!known.data?.merchants.some((m) => m.display === payee);
  const close = () => { setOpen(false); setPick(""); setTarget(null); setErr(null); };
  const merge = useMutation({
    mutationFn: (to: string) => api.merchantRename({ display: payee, to }),
    onMutate: () => setErr(null),
    onSuccess: (_r, to) => {
      // the source name is gone from every merchant list the moment the
      // server answers — both the catalog shape and the bill picker's flat
      // list live under ["merchants"]
      patchQueries<{ merchants: (string | { display: string })[] }>(
        qc, ["merchants"], (old) => Array.isArray(old.merchants)
          ? { ...old, merchants: old.merchants.filter((m) =>
              (typeof m === "string" ? m : m.display) !== payee) }
          : old);
      // what the merge moved: the target's counts and detail, every ledger
      // row that printed the old name, the bill that followed it
      qc.invalidateQueries({ queryKey: ["merchants"] });
      qc.invalidateQueries({ queryKey: ["merchant-detail"] });
      qc.invalidateQueries({ queryKey: ["txns"] });
      qc.invalidateQueries({ queryKey: ["bills"] });
      qc.invalidateQueries({ queryKey: ["bills-history", to] });
      qc.invalidateQueries({ queryKey: ["today"] });
      nav(`/bills/history?payee=${encodeURIComponent(to)}`, { replace: true });
      // the source's history names a merchant that no longer exists; dropped
      // after the page has moved off it, so nothing refetches it
      qc.removeQueries({ queryKey: ["bills-history", payee] });
    },
    onError: (e) => setErr(errText(e)),
  });
  if (!isMerchant && !open) return null;
  if (!open) return (
    <div style={{ marginTop: ".7rem" }}>
      <button onClick={() => setOpen(true)}>Merge into…</button>
    </div>
  );
  return (
    <div style={{ marginTop: ".7rem", fontSize: 13 }}>
      {target ? (
        <div className="note" style={{ display: "block" }}>
          <div>Rows named <b>{payee}</b> will be filed under <b>{target}</b>;
            the bill, if any, follows.</div>
          <div style={{ marginTop: ".5rem", display: "flex", gap: ".5rem" }}>
            <button className="pri" disabled={merge.isPending}
                    onClick={() => merge.mutate(target)}>
              {merge.isPending ? "merging…" : "Yes, merge"}</button>
            <button disabled={merge.isPending}
                    onClick={() => setTarget(null)}>Back</button>
          </div>
        </div>
      ) : (
        <>
          <div style={{ display: "flex", gap: ".4rem", alignItems: "center", flexWrap: "wrap" }}>
            <input autoFocus value={pick} placeholder="merge into which merchant?"
                   aria-label="merge into" onChange={(e) => setPick(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Escape") close(); }}
                   style={{ width: "min(18rem, 100%)" }} />
            <button onClick={close}>Cancel</button>
          </div>
          <div style={{ display: "flex", gap: ".35rem", flexWrap: "wrap", marginTop: ".4rem" }}>
            {options.map((o) => (
              <button key={o.display} type="button" className="chip"
                      onClick={() => setTarget(o.display)}>
                <MerchantAvatar name={o.display} logo={o.logo} size={14} />{" "}
                {o.display}
                <span className="mut" style={{ fontSize: 11 }}> · {o.rows}</span>
              </button>
            ))}
            {pickQ && !cands.isPending && options.length === 0 && (
              <span className="sub">No merchant matches that.</span>
            )}
          </div>
        </>
      )}
      {err && <div className="bad" style={{ marginTop: ".4rem" }}>{err}</div>}
    </div>
  );
}

function RestoreArchived({ payee, onDone }: { payee: string; onDone: () => void }) {
  const qc = useQueryClient();
  const [err, setErr] = useState<string | null>(null);
  const restore = useMutation({
    mutationFn: () => api.billsRestore(payee),
    onMutate: () => setErr(null),
    onSuccess: () => {
      removeFromList<BillsData, BillsData["archived"][number]>(
        qc, ["bills"], "archived", (b) => b.payee === payee);
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["calendar"] });
      onDone();
    },
    onError: (e) => setErr(errText(e)),
  });
  return (
    <div style={{ marginTop: ".7rem", display: "flex", gap: ".5rem",
                  alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
      <span className="mut">This bill is archived.</span>
      <button className="pri" disabled={restore.isPending}
              onClick={() => restore.mutate()}>
        {restore.isPending ? "restoring…" : "Restore"}</button>
      {err && <span className="bad">{err}</span>}
    </div>
  );
}

function AddAsBill({ d, onDone }: { d: BillsHistoryData; onDone: () => void }) {
  const [amount, setAmount] = useState(d.fit ? d.fit.amount.toFixed(2) : "");
  const [cadence, setCadence] = useState(d.fit?.cadence ?? "MONTHLY:1");
  const [nextDue, setNextDue] = useState(d.fit?.next_due ?? "");
  const [err, setErr] = useState<string | null>(null);
  // "Custom interval…" → any FREQ:interval (every X days … years) for
  // unusual bills. The engine schedules + budgets any
  // interval already, so this just widens the picker.
  const [customN, setCustomN] = useState("2");
  const [customUnit, setCustomUnit] = useState("YEARLY");
  const effCadence = cadence === "CUSTOM"
    ? customCadence(customUnit, customN) : cadence;
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
      // a mostly-inflow merchant (a paycheck) is recurring income; saved
      // as a bill it would wait on a charge that never comes
      income: !!d.inflow,
    }),
    onSuccess: onDone,
    onError: (e) => setErr(errText(e)),
  });
  return (
    <form style={{ marginTop: ".7rem", display: "flex", gap: ".5rem",
                   alignItems: "center", flexWrap: "wrap", fontSize: 13 }}
          onSubmit={(e) => { e.preventDefault(); add.mutate(); }}>
      <strong>{d.inflow ? "Add as recurring income:"
                        : "Add as recurring bill:"}</strong>
      $<input type="number" step="0.01" required style={{ width: "7rem" }}
              value={amount} onChange={(e) => setAmount(e.target.value)} />
      <select value={cadence} onChange={(e) => setCadence(e.target.value)}>
        {/* a ledger suggestion may be an unusual cadence not in the short
            list — keep it selectable/visible rather than silently
            mis-showing, and still there after another option is tried */}
        {cadenceChoices(SIMPLE_CADENCES,
                        [d.fit?.cadence, cadence === "CUSTOM" ? null : cadence])
          .map(([v, label]) => (
            <option key={v} value={v}>{label}</option>
          ))}
        <option value="CUSTOM">Custom interval…</option>
      </select>
      {cadence === "CUSTOM" && (
        <span>every <input type="number" min="1" max={customMax(customUnit)} style={{ width: "3.5rem" }}
               value={customN} onChange={(e) => setCustomN(e.target.value)} />
          <select value={customUnit}
                  onChange={(e) => setCustomUnit(e.target.value)}>
            {CUSTOM_UNITS.map(([v, label]) => (
              <option key={v} value={v}>{label}</option>
            ))}
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
  const cats = useQuery({ queryKey: ["categories"], queryFn: api.categories });
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
