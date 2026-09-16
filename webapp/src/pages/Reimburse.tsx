import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, errText, mmdd, money, toast, type PendingReimb, type ReimbPair }
  from "../api/client";
import { patchList, removeFromList } from "../api/cache";
import { canEdit } from "../role";

// How long this charge has been out of pocket. Stating it, rather than
// leaving it to be worked out from the date column, turns a list into a
// to-do.
function waited(iso: string): number {
  const d = new Date(iso + "T00:00:00");
  return Math.max(0, Math.round((Date.now() - d.getTime()) / 86400000));
}
function waitedLabel(iso: string): string {
  const n = waited(iso);
  return n === 0 ? "today" : n === 1 ? "waiting 1 day" : `waiting ${n} days`;
}

export default function Reimburse() {
  const q = useQuery({ queryKey: ["reimb"], queryFn: api.reimbPending });
  // matching / unflagging / partial marks are editing writes — viewers
  // keep the read view of what's awaiting
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  const [open, setOpen] = useState<string | null>(null);

  if (q.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;
  if (q.isError)
    return <div className="card">Couldn't load: {String(q.error)}</div>;

  const pending = q.data.pending;

  // The page's own number. "How much am I owed?" is the reason this list
  // exists, so the page states it rather than leaving it to be added up.
  const owed = pending.reduce((a, t) => a + Math.abs(t.amount)
    - (t.received ?? 0), 0);
  const oldest = pending.reduce((a, t) => Math.max(a, waited(t.date)), 0);

  return (
    <>
      <h1>Reimbursements</h1>
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", gap: ".9rem", alignItems: "baseline",
                      flexWrap: "wrap" }}>
          <span style={{ fontSize: "1.7rem", fontWeight: 700 }}>{money(owed)}</span>
          <span className="sub">owed back across {pending.length} charge
            {pending.length === 1 ? "" : "s"}</span>
          {oldest > 0 && <span className="sub">· oldest {waitedLabel(
            pending.reduce((a, t) => waited(t.date) > waited(a.date) ? t : a,
                           pending[0]).date)}</span>}
          <Link className="btn" to="/transactions"
                style={{ marginLeft: "auto" }}>‹ transactions</Link>
        </div>
        <div className="sub" style={{ marginTop: ".2rem" }}>Flagged charges
          still count as real spending until their deposit is matched — that's
          why they're here.</div>
      </div>

      {pending.length === 0 ? (
        <div className="card">
          <p className="sub" style={{ margin: 0 }}>Nothing waiting — flag a
            charge from the ⋯ menu on the Transactions page
            (⚑ reimbursement expected later).</p>
        </div>
      ) : (
        <div className="card">
          <table>
            <thead>
              <tr><th style={{ width: "1%", whiteSpace: "nowrap" }}>Date</th>
                  <th className="num" style={{ width: "1%" }}>Amount</th>
                  <th>Merchant</th>
                  <th className="hide-m">Account</th>
                  <th style={{ width: "1%" }}></th></tr>
            </thead>
            <tbody>
              {pending.map((t) => (
                <PendingRow key={t.id} t={t} mayEdit={mayEdit}
                            open={mayEdit && open === t.id}
                            onToggle={() => setOpen(open === t.id ? null : t.id)}
                            onDone={(m) => { toast(m); setOpen(null); }} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <MatchedPairs mayEdit={mayEdit} onNotice={toast} />
    </>
  );
}

// The undo surface. A wrong match stamps both sides as transfers and takes
// the charge off the pending list above, so without this list the mistake
// is invisible everywhere the person would look.
function MatchedPairs({ mayEdit, onNotice }: {
  mayEdit: boolean; onNotice: (m: string) => void;
}) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["reimb-pairs"], queryFn: api.reimbPairs });
  const unlink = useMutation({
    mutationFn: (p: ReimbPair) =>
      api.reimbUnlink(p.expense_id, p.reimburse_id),
    onSuccess: (_r, p) => {
      removeFromList<{ pairs: ReimbPair[] }, ReimbPair>(
        qc, ["reimb-pairs"], "pairs",
        (x) => x.expense_id === p.expense_id
            && x.reimburse_id === p.reimburse_id);
      // both sides fell back to their native categories, so the money
      // pages and the pending list (a partial's progress reversed) moved
      qc.invalidateQueries({ queryKey: ["reimb"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["txns"] });
      onNotice(`Unmatched ${p.expense_payee} — both sides count as real `
               + "money again.");
    },
    onError: (e) => onNotice(errText(e)),
  });
  if (!q.data || q.data.pairs.length === 0) return null;
  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Matched</h2>
      <div className="sub" style={{ marginTop: "-.4rem" }}>Each pair nets a
        charge against the deposit that paid it back. Undo a wrong match and
        both sides count as real spending and income again.</div>
      <table style={{ marginTop: ".5rem" }}>
        <thead>
          <tr><th style={{ width: "1%", whiteSpace: "nowrap" }}>Date</th>
              <th>Charge</th>
              <th>Paid back by</th>
              <th className="num hide-m">Amount</th>
              <th style={{ width: "1%" }}></th></tr>
        </thead>
        <tbody>
          {q.data.pairs.map((p) => (
            <tr key={p.expense_id + p.reimburse_id}>
              <td className="mut" style={{ whiteSpace: "nowrap" }}>
                {mmdd(p.expense_date)}</td>
              <td>{p.expense_payee}
                {!!p.partial && <span className="pill m"
                  style={{ marginLeft: ".4rem" }}
                  title={`partial — ${money(p.received ?? 0)} of this charge is netted`}>
                  partial</span>}</td>
              <td>{p.deposit_payee}
                <span className="mut"> · {mmdd(p.deposit_date)}</span></td>
              <td className="num hide-m">{money(Math.abs(
                p.partial ? (p.received ?? 0) : p.expense_amount))}</td>
              <td>{mayEdit && (
                <button disabled={unlink.isPending}
                        title="unlink this pair — the charge returns to real spending, the deposit to real income"
                        onClick={() => unlink.mutate(p)}>undo match</button>
              )}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function PendingRow({ t, mayEdit, open, onToggle, onDone }: {
  t: PendingReimb; mayEdit: boolean; open: boolean; onToggle: () => void;
  onDone: (msg: string) => void;
}) {
  const qc = useQueryClient();
  // Turning "partial" on lives in the ⋯ strip; a row that IS partial shows
  // its progress instead. A permanent checkbox plus a naked $ input in the
  // merchant cell of EVERY row would be form controls on hundreds of rows
  // for a state most never enter.
  const [menu, setMenu] = useState(false);
  const [editExp, setEditExp] = useState(false);
  type Pending = { pending: PendingReimb[] };
  const unflag = useMutation({
    mutationFn: () => api.reimbUnflag(t.id),
    onSuccess: () => {
      // the row leaves the list now — the server has already dropped the
      // flag; waiting on a refetch to re-prove that left it sitting there.
      // The charge is ordinary spend again, so the ledger and the verdict
      // both move.
      removeFromList<Pending, PendingReimb>(qc, ["reimb"], "pending",
                                            (x) => x.id === t.id);
      qc.invalidateQueries({ queryKey: ["txns"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      onDone(`Unflagged ${t.payee}.`);
    },
  });
  const setPartial = useMutation({
    mutationFn: (v: { partial: boolean; expected: number | null }) =>
      api.reimbFlag(t.id, v),
    // the pill and the progress line read straight from the cached row, so
    // write the new marking there first and put the old one back only if
    // the server refuses — otherwise the toggle snaps back until a refetch
    onMutate: async (v) => {
      await qc.cancelQueries({ queryKey: ["reimb"] });
      const prev = qc.getQueryData<Pending>(["reimb"]);
      patchList<Pending, PendingReimb>(qc, ["reimb"], "pending",
        (x) => x.id === t.id,
        { partial: v.partial ? 1 : 0, expected: v.expected });
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["reimb"], ctx.prev);
    },
    onSuccess: () => {
      setEditExp(false);
      // the Transactions page shows the partial marking on its rows
      qc.invalidateQueries({ queryKey: ["txns"] });
    },
  });

  const got = t.received ?? 0;
  const expect = t.expected ?? Math.abs(t.amount);
  const pct = expect > 0 ? Math.min(100, Math.round((got / expect) * 100)) : 0;

  return (
    <>
      <tr>
        <td className="mut" style={{ whiteSpace: "nowrap" }}>{mmdd(t.date)}</td>
        <td className="num" style={{ whiteSpace: "nowrap" }}>{money(-t.amount)}</td>
        <td>{t.payee}
          {t.pending ? <span className="pill m" style={{ marginLeft: 6 }}>pend</span> : null}
          {!!t.partial && (
            <span className="pill m" style={{ marginLeft: 6 }}>partial</span>)}
          {!t.partial && (
            <span className="sub"> · {waitedLabel(t.date)}</span>)}
          {!!t.partial && (
            <div className="sub" style={{ display: "flex", gap: ".5rem",
                marginTop: ".2rem", alignItems: "center", flexWrap: "wrap" }}>
              <span>got {money(got)} of {money(expect)}</span>
              <span className="minibar" style={{ width: "7rem" }}>
                <i style={{ width: `${pct}%` }} /></span>
              {mayEdit && !editExp && (
                <a href="#" onClick={(e) => { e.preventDefault(); setEditExp(true); }}
                >edit expected</a>)}
              {mayEdit && editExp && (
                <input inputMode="decimal" autoFocus size={7}
                  defaultValue={t.expected ?? ""} placeholder="expect $"
                  style={{ fontSize: 12, padding: "0 .3rem", minHeight: 0 }}
                  onKeyDown={(e) => e.key === "Enter" &&
                    (e.target as HTMLInputElement).blur()}
                  onBlur={(e) => {
                    const v = Number(e.target.value.replace(/[$,\s]/g, ""));
                    setPartial.mutate({ partial: true, expected: v > 0 ? v : null });
                  }} />)}
              <span>· {waitedLabel(t.date)}</span>
            </div>)}
        </td>
        <td className="mut hide-m" style={{ whiteSpace: "nowrap", maxWidth: "15rem",
            overflow: "hidden", textOverflow: "ellipsis" }}>{t.account ?? ""}</td>
        <td style={{ whiteSpace: "nowrap" }}>
          {mayEdit && (<>
          <button className="pri" onClick={onToggle}>
            {open ? "close" : "match…"}
          </button>{" "}
          <button className="row-menu" aria-expanded={menu}
                  aria-label={`actions for ${t.payee}`}
                  onClick={() => setMenu(!menu)}>⋯</button>
          </>)}
        </td>
      </tr>
      {mayEdit && menu && (
        <tr className="tr-expand"><td colSpan={5}>
          <div className="row-actions">
            <button disabled={setPartial.isPending}
              title="a fraction comes back (co-pay, insurance claim); the rest stays real spend"
              onClick={() => setPartial.mutate({
                partial: !t.partial,
                expected: t.partial ? null : (t.expected ?? null) })}>
              {t.partial ? "✓ partial reimbursement" : "only part comes back"}
            </button>
            <button onClick={() => unflag.mutate()} disabled={unflag.isPending}
                    title="remove from this list without matching">
              remove from this list</button>
          </div>
        </td></tr>
      )}
      {open && (
        <tr>
          <td colSpan={5} style={{ background: "var(--hover)" }}>
            <Matcher anchor={t} onDone={onDone} />
          </td>
        </tr>
      )}
    </>
  );
}

// Why this deposit is a candidate at all — bare rows leave the reader to
// compare the amounts and dates themselves, on every row.
function why(c: { amount: number; date: string },
             anchor: PendingReimb): string {
  const gap = Math.abs(Math.abs(c.amount) - Math.abs(anchor.amount));
  const days = Math.round(
    (new Date(c.date + "T00:00:00").getTime()
     - new Date(anchor.date + "T00:00:00").getTime()) / 86400000);
  const amt = gap < 0.005 ? "exact amount"
    : Math.abs(c.amount) < Math.abs(anchor.amount)
      ? `${money(gap)} short` : `${money(gap)} over`;
  const when = days <= 0 ? "same day or earlier"
    : days === 1 ? "1 day later" : `${days} days later`;
  return `${amt} · ${when}`;
}

function Matcher({ anchor, onDone }: {
  anchor: PendingReimb; onDone: (msg: string) => void;
}) {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const cand = useQuery({
    queryKey: ["reimb-cand", anchor.id, search],
    queryFn: () => api.reimbCandidates(anchor.id, search),
  });
  const [picked, setPicked] = useState<Set<string>>(new Set());
  // a partial match nets only the received amount off the charge;
  // the remainder stays real spend. Defaults from the flag's own marking.
  const [partial, setPartial] = useState(!!anchor.partial);

  const link = useMutation({
    mutationFn: (v: { ids: string[]; partial: boolean; amount: number }) =>
      api.reimbLink(anchor.id, v.ids, v.partial),
    onSuccess: (r, v) => {
      // settle the anchor row in the cache now rather than leaving it
      // behind the closed matcher until the refetch lands: a full match
      // takes it off the list, a partial one advances its progress. The
      // refetch still runs to pick up what the server decided (a partial
      // that reached its expected amount, a link that failed).
      if (r.linked > 0) {
        type Pending = { pending: PendingReimb[] };
        if (v.partial)
          patchList<Pending, PendingReimb>(qc, ["reimb"], "pending",
            (x) => x.id === anchor.id,
            (x) => ({ ...x, received: (x.received ?? 0) + v.amount }));
        else
          removeFromList<Pending, PendingReimb>(qc, ["reimb"], "pending",
                                                (x) => x.id === anchor.id);
      }
      qc.invalidateQueries({ queryKey: ["reimb"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["txns"] });
      onDone(r.errors?.length
        ? `Linked ${r.linked}, ${r.errors.length} failed: ${r.errors.join("; ")}`
        : `Linked ${anchor.payee} to ${r.linked} deposit${r.linked === 1 ? "" : "s"}.`);
    },
    onError: (e) => onDone(errText(e)),
  });

  const toggle = (id: string) => {
    const n = new Set(picked);
    if (n.has(id)) n.delete(id); else n.add(id);
    setPicked(n);
  };
  const target = Math.abs(anchor.amount);
  const sum = cand.data
    ? cand.data.candidates.filter((c) => picked.has(c.id))
        .reduce((s, c) => s + Math.abs(c.amount), 0)
    : 0;

  return (
    <div className="matcher">
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap", alignItems: "center" }}>
        <b>Deposits near {money(target)} since {mmdd(anchor.date)} — pick the
          one that paid this back</b>
        <input placeholder="filter by merchant…" size={18} value={search}
               style={{ marginLeft: "auto" }}
               onChange={(e) => setSearch(e.target.value)} />
      </div>
      <div className="sub" style={{ marginTop: ".3rem" }}>Tick every deposit
        that covers this charge — one deposit can reimburse many charges.</div>
      {/* action row ABOVE the candidates: a long list would push the
          match button below the fold */}
      <div style={{ marginTop: ".7rem", display: "flex", gap: ".8rem",
                    alignItems: "center", flexWrap: "wrap" }}>
        <button className="pri" disabled={picked.size === 0 || link.isPending}
                onClick={() => link.mutate({ ids: [...picked], partial, amount: sum })}>
          {picked.size > 1 ? `match ${picked.size} selected` : "match selected"}
        </button>
        <label className="sub" style={{ cursor: "pointer" }}
          title="partial: only the received amount nets off the charge — the remainder stays real spend">
          <input type="checkbox" checked={partial}
                 style={{ verticalAlign: "middle" }}
                 onChange={(e) => setPartial(e.target.checked)} />
          {" "}partial reimbursement
        </label>
        {picked.size > 0 && (
          <span className="sub">selected {money(sum)} of {money(target)}
            {Math.abs(sum - target) < 0.5 ? " — exact ✓"
              : partial ? ` — ${money(Math.max(0, target - sum))} stays as spend` : ""}</span>
        )}
      </div>
      {cand.isPending && <p className="mut">finding deposits…</p>}
      {cand.data && cand.data.candidates.length === 0 && (
        <p className="mut">No opposite-direction transactions found near this
          charge — the deposit may not have landed yet.</p>
      )}
      {cand.data && cand.data.candidates.length > 0 && (
        <table style={{ marginTop: ".5rem" }}>
          <thead>
            <tr><th style={{ width: "1%" }}></th>
                <th style={{ width: "1%", whiteSpace: "nowrap" }}>Date</th>
                <th className="num" style={{ width: "1%" }}>Amount</th>
                <th>Merchant</th>
                <th className="hide-m">why</th></tr>
          </thead>
          <tbody>
            {cand.data.candidates.map((c) => (
              <tr key={c.id} style={{ cursor: "pointer" }} onClick={() => toggle(c.id)}>
                <td style={{ whiteSpace: "nowrap" }}>
                  <input type="checkbox" checked={picked.has(c.id)}
                         onClick={(e) => e.stopPropagation()}
                         onChange={() => toggle(c.id)} />
                </td>
                <td className="mut" style={{ whiteSpace: "nowrap" }}>{mmdd(c.date)}</td>
                <td className={`num ${c.amount < 0 ? "pos" : ""}`}
                    style={{ whiteSpace: "nowrap" }}>{money(-c.amount)}</td>
                <td>{c.payee}</td>
                <td className="sub hide-m" style={{ whiteSpace: "nowrap" }}>
                  {why(c, anchor)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
