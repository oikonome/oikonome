import { keepPreviousData, useInfiniteQuery, useMutation, useQuery,
         useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, errText, mmdd, money, toast, type PendingReimb, type ReimbPair,
         type ReimbPairsPage } from "../api/client";
import { patchList, patchQueries, removeFromList } from "../api/cache";
import { canEdit } from "../role";
import { pairGapHint, reimbCandidateWhy, reimbLeft, reimbNeed, reimbOwed }
  from "../moneymath";

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
  // `?match=<id>` arrives from a transaction's "mark reimbursed…" and
  // opens that charge's candidate deposits straight away
  const [params] = useSearchParams();
  const [open, setOpen] = useState<string | null>(() => params.get("match"));

  if (q.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;
  // a failed REFETCH keeps the data it had — only a first load that
  // failed has nothing to show
  if (q.isLoadingError)
    return <div className="card">Couldn't load: {String(q.error)}</div>;

  const pending = q.data.pending;

  // The page's own number. "How much am I owed?" is the reason this list
  // exists, so the page states it rather than leaving it to be added up.
  // Each charge counts what is still owed against its expected amount
  // (reimbOwed), the same figure the Today strip and the email state.
  const owed = reimbOwed(pending);
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
// is invisible everywhere the person would look. It pages back ("show
// more") and searches by either side's merchant: a wrong pair older than
// the newest page could otherwise never be undone.
function MatchedPairs({ mayEdit, onNotice }: {
  mayEdit: boolean; onNotice: (m: string) => void;
}) {
  const qc = useQueryClient();
  const [typed, setTyped] = useState("");
  const [search, setSearch] = useState("");
  // one request per pause in typing, not per keystroke
  useEffect(() => {
    const id = setTimeout(() => setSearch(typed.trim()), 300);
    return () => clearTimeout(id);
  }, [typed]);
  const q = useInfiniteQuery({
    queryKey: ["reimb-pairs", search],
    queryFn: ({ pageParam }) => api.reimbPairs(pageParam, search),
    initialPageParam: 1,
    getNextPageParam: (last: ReimbPairsPage, all) =>
      last.more ? all.length + 1 : undefined,
    // the list stays up while a narrower search loads
    placeholderData: keepPreviousData,
  });
  const pairs = q.data?.pages.flatMap((pg) => pg.pairs) ?? [];
  const unlink = useMutation({
    mutationFn: (p: ReimbPair) =>
      api.reimbUnlink(p.expense_id, p.reimburse_id),
    onSuccess: (_r, p) => {
      // every cached search and page drops the row at once
      patchQueries<InfiniteData<ReimbPairsPage>>(qc, ["reimb-pairs"],
        (old) => ({ ...old, pages: old.pages.map((pg) => ({
          ...pg, pairs: pg.pairs.filter((x) => !(
            x.expense_id === p.expense_id
            && x.reimburse_id === p.reimburse_id)) })) }));
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
  // nothing ever matched: no card. A search with no hits keeps the card
  // (and its search box) so the search can be changed.
  if (!q.data || (pairs.length === 0 && !search && !typed)) return null;
  return (
    <div className="card">
      <div style={{ display: "flex", gap: ".5rem", alignItems: "center",
                    flexWrap: "wrap" }}>
        <h2 style={{ margin: 0 }}>Matched</h2>
        <input placeholder="find a pair by merchant…" size={22} value={typed}
               aria-label="find a matched pair by merchant"
               style={{ marginLeft: "auto" }}
               onChange={(e) => setTyped(e.target.value)} />
      </div>
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
          {pairs.length === 0 && (
            <tr><td colSpan={5} className="mut">No matched pair names
              “{search}”.</td></tr>)}
          {pairs.map((p) => (
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
      {q.hasNextPage && (
        <button style={{ marginTop: ".5rem" }}
                disabled={q.isFetchingNextPage}
                onClick={() => q.fetchNextPage()}>
          {q.isFetchingNextPage ? "loading…" : "show more"}</button>
      )}
      {q.isFetchNextPageError && (
        <span className="note bad" style={{ marginLeft: ".5rem" }}>
          Couldn't load more: {errText(q.error)}</span>
      )}
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
    // the button simply re-enabling would read as "nothing happened"
    onError: (e) => toast(`Couldn't unflag ${t.payee}: ${errText(e)}`),
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
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["reimb"], ctx.prev);
      // the rollback alone looks like the toggle (or the typed expected
      // amount) was ignored; say the save failed and why
      toast(`Couldn't save ${t.payee}: ${errText(e)}`);
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

function Matcher({ anchor, onDone }: {
  anchor: PendingReimb; onDone: (msg: string) => void;
}) {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const cand = useQuery({
    queryKey: ["reimb-cand", anchor.id, search],
    queryFn: () => api.reimbCandidates(anchor.id, search),
  });
  // Ticked deposits by id, with their amounts. A filter change swaps the
  // visible list but not the selection, so the running total and the
  // count come from here — summing only the visible rows left a ticked
  // deposit the filter now hides out of "selected $X", while the button
  // still linked it.
  const [picked, setPicked] = useState<Map<string, number>>(new Map());
  // a partial match nets only the received amount off the charge;
  // the remainder stays real spend. Defaults from the flag's own marking.
  const [partial, setPartial] = useState(!!anchor.partial);
  // A failed link stays inside the matcher: closing it (as a success does)
  // would throw away the ticked deposits over a blip or a rate limit, and
  // the retry is the same button.
  const [linkErr, setLinkErr] = useState<string | null>(null);

  const link = useMutation({
    mutationFn: (v: { ids: string[]; partial: boolean }) =>
      api.reimbLink(anchor.id, v.ids, v.partial),
    onSuccess: (r, v) => {
      // Notes are outcomes, not refusals: the server links a full match
      // as partial when the deposit is bigger than the charge it repays,
      // because the rest of that deposit is still income.
      const notes = r.notes ?? [];
      // A clean full match takes the charge off the list now rather than
      // leaving it behind the closed matcher until the refetch lands. A
      // partial link (asked for, or downgraded by the server) is not
      // patched: what it netted is min(the charge's remaining need, each
      // deposit's remaining balance), decided server-side and not in the
      // response, so the deposits' face value would overstate it — the
      // refetch brings the real progress.
      if (r.linked > 0 && !v.partial && notes.length === 0)
        removeFromList<{ pending: PendingReimb[] }, PendingReimb>(
          qc, ["reimb"], "pending", (x) => x.id === anchor.id);
      qc.invalidateQueries({ queryKey: ["reimb"] });
      // the new pair belongs in the Matched card, the undo surface
      qc.invalidateQueries({ queryKey: ["reimb-pairs"] });
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["txns"] });
      const head = r.errors?.length
        ? `Linked ${r.linked}, ${r.errors.length} failed: ${r.errors.join("; ")}`
        : `Linked ${anchor.payee} to ${r.linked} deposit${r.linked === 1 ? "" : "s"}.`;
      onDone(notes.length ? `${head} ${notes.join(" ")}` : head);
    },
    onMutate: () => setLinkErr(null),
    onError: (e) => setLinkErr(errText(e)),
  });

  // each ticked row counts what it has LEFT to give (reimbLeft), the
  // figure the server links by
  const toggle = (c: { id: string; amount: number;
                       left_amount?: number | null }) => {
    const n = new Map(picked);
    if (n.has(c.id)) n.delete(c.id); else n.set(c.id, reimbLeft(c));
    setPicked(n);
  };
  const target = Math.abs(anchor.amount);
  // What the charge still needs and what already came back, from the
  // candidates door (the pending row's own `received` until it answers):
  // a charge partly repaid is matched against the rest, not its face.
  const live = { ...anchor, left_amount: cand.data?.anchor?.left_amount
                   ?? Math.abs(anchor.amount) - (anchor.received ?? 0),
                 received: cand.data?.anchor?.received ?? anchor.received };
  const need = reimbNeed(live);
  const sum = [...picked.values()].reduce((s, a) => s + a, 0);
  const shown = new Set((cand.data?.candidates ?? []).map((c) => c.id));
  const hiddenPicked = cand.data
    ? [...picked.keys()].filter((id) => !shown.has(id)).length : 0;

  return (
    <div className="matcher">
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap", alignItems: "center" }}>
        {/* the candidate search spans 180 days either side of the charge,
            so the heading states the window rather than a start date */}
        <b>Deposits within 180 days of this {money(target)} charge — exact
          amounts first, then the closest dates</b>
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
                onClick={() => link.mutate({ ids: [...picked.keys()], partial })}>
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
          <span className="sub">selected {money(sum, true)} of{" "}
            {money(need, true)}{(live.received ?? 0) > 0.005 && anchor.amount > 0
              ? " still owed" : ""}
            {pairGapHint([...picked.values()], live, partial)}
            {hiddenPicked > 0 && ` (${hiddenPicked} ticked but hidden by the filter)`}</span>
        )}
      </div>
      {linkErr && (
        <div className="note bad" style={{ marginTop: ".5rem" }}
             onClick={() => setLinkErr(null)}>{linkErr}</div>
      )}
      {cand.isPending && <p className="mut">finding deposits…</p>}
      {/* a failed search must say so — otherwise neither the list nor the
          "nothing found" line renders and the matcher is just blank */}
      {cand.isError && !cand.data && (
        <p className="note bad">Couldn't search for deposits:{" "}
          {errText(cand.error)}{" "}
          <button className="linklike" onClick={() => cand.refetch()}>
            retry</button></p>
      )}
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
              <tr key={c.id} style={{ cursor: "pointer" }} onClick={() => toggle(c)}>
                <td style={{ whiteSpace: "nowrap" }}>
                  <input type="checkbox" checked={picked.has(c.id)}
                         onClick={(e) => e.stopPropagation()}
                         onChange={() => toggle(c)} />
                </td>
                <td className="mut" style={{ whiteSpace: "nowrap" }}>{mmdd(c.date)}</td>
                <td className={`num ${c.amount < 0 ? "pos" : ""}`}
                    style={{ whiteSpace: "nowrap" }}>{money(-c.amount)}</td>
                {/* the list spans every checking and savings account plus
                    the charge's own card, so each row names where the
                    money landed, and a pending deposit says so */}
                <td>{c.payee}
                  {c.pending ? <span className="pill m" style={{ marginLeft: 6 }}>pend</span> : null}
                  {c.account && <div className="sub">{c.account}</div>}</td>
                <td className="sub hide-m" style={{ whiteSpace: "nowrap" }}>
                  {reimbCandidateWhy(c, live)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
