// Receipts snapped before their transaction reached the ledger. Snap one at
// the till; it parses like any receipt, waits here, and the server pairs it
// with its charge when the charge arrives. What the matcher cannot decide
// alone — two charges that fit equally, a receipt with nothing read off it
// — is offered here as candidates to match with one tap. A receipt nothing
// will read (no AI on the instance, or a read that failed) takes its total
// and date typed in, and is matched the moment they are saved.
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, errText, mmdd, money, type WaitingReceipts as Waiting,
         type WaitingReceipt } from "../api/client";
import { patchList, removeFromList } from "../api/cache";
import { WAITING_KEY, moreWaiting, receiptDateBounds, seedWaiting,
         useMatchReceipt,
         useMoreWaiting, useReceiptDetails, useUnmatchReceipt,
         useWaitingReceipts } from "../api/waiting";
import { localYmd, receiptDetailsDraft } from "../moneymath";
import { canEdit } from "../role";

/** The camera-or-file button that snaps a receipt with no transaction. */
export function SnapReceiptButton({ label = "📷 Receipt" }:
                                  { label?: string }) {
  const qc = useQueryClient();
  const snap = useMutation({
    mutationFn: (f: File) => api.receiptSnap(f),
    // the door answers with the whole waiting list — seed it, so the new
    // receipt (and its "reading…" state) is on screen now
    onSuccess: (r) => {
      const { id: _id, ...list } = r;
      seedWaiting(qc, list);
      // the waiting list lives on the Receipts page; from anywhere else the
      // only sign of success would be a badge count, so say it
      if (!window.location.pathname.endsWith("/items"))
        window.dispatchEvent(new CustomEvent("oiko-toast", { detail: {
          text: "Receipt saved — it's being read and waits on the Receipts page until its charge arrives",
          to: "/items" } }));
    },
    onError: (e) => window.dispatchEvent(new CustomEvent("oiko-toast",
      { detail: `Couldn't save the receipt: ${errText(e)}` })),
  });
  const onPick = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (f) snap.mutate(f);
    e.target.value = "";
  };
  // one button. Without `capture` a phone browser offers its own sheet —
  // camera, photo library, files — so a receipt photographed earlier is as
  // reachable as one taken now; a desktop gets a file picker. (`capture`
  // would open the camera straight away and hide the library.)
  return (
    <label className="btn" style={{ cursor: "pointer" }}
           title="photograph a receipt now, or pick a photo or PDF you already have — it matches its transaction when the charge reaches the ledger">
      {snap.isPending ? "uploading…" : label}
      <input type="file" accept="image/*,.pdf"
        style={{ display: "none" }} onChange={onPick} />
    </label>
  );
}

/** "N waiting" — a link to the list, for wherever receipts are surfaced. */
export function WaitingBadge() {
  const q = useWaitingReceipts();
  const n = q.data?.count ?? 0;
  if (!n) return null;
  return (
    <Link to="/items" className="pill m" title="receipts waiting for their transaction">
      {n} receipt{n === 1 ? "" : "s"} waiting</Link>
  );
}

function Thumb({ r }: { r: { id: string; mime: string; has_optimized: boolean } }) {
  const href = `/api/receipts/${r.id}/image`;
  if (r.mime === "application/pdf") {
    return <a className="btn" href={href} target="_blank" rel="noreferrer"
              style={{ fontSize: 12, padding: ".15rem .5rem" }}>PDF</a>;
  }
  return (
    <a href={href} target="_blank" rel="noreferrer" title="open the receipt">
      <img src={r.has_optimized ? `${href}?variant=optimized` : href}
           alt="receipt" loading="lazy"
           style={{ width: 48, height: 64, objectFit: "cover",
                    borderRadius: 4, border: "1px solid var(--line)",
                    display: "block" }} />
    </a>
  );
}

function statusWords(r: WaitingReceipt): { cls: string; text: string } {
  if (r.status === "parsing") return { cls: "m", text: "reading…" };
  if (r.status === "failed") return { cls: "r", text: "couldn't read" };
  if (r.needs_details) return { cls: "a", text: "needs its total" };
  if (r.status === "uploaded") return { cls: "m", text: "not read yet" };
  return { cls: "m", text: "waiting for a transaction" };
}

/** "Enter the total and date": what the AI could not read (or there is no
 *  AI to read it), typed in. Saving asks the server to match it at once;
 *  the row then shows its candidates, or leaves the list when it paired. */
function DetailsForm({ r, onDone }: { r: WaitingReceipt; onDone?: () => void }) {
  const save = useReceiptDetails();
  const [total, setTotal] = useState(r.total != null ? r.total.toFixed(2) : "");
  const [date, setDate] = useState(r.date ?? localYmd(new Date()));
  const [store, setStore] = useState(r.merchant ?? "");
  const [err, setErr] = useState<string | null>(null);
  // the server takes a date from a year before the snap to the day after;
  // the picker offers exactly that, and a typed date outside it is told
  // here instead of coming back as a refusal
  const bounds = receiptDateBounds(r.created_at);
  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const d = receiptDetailsDraft(total, date, store);
    if (!d.ok) { setErr(d.error); return; }
    if (bounds && (d.body.date < bounds.min || d.body.date > bounds.max)) {
      setErr(d.body.date > bounds.max
        ? "That date is after the receipt was snapped"
        : "That date is more than a year before the receipt was snapped");
      return;
    }
    setErr(null);
    save.mutate({ id: r.id, body: d.body }, { onSuccess: () => onDone?.() });
  };
  return (
    <form onSubmit={submit} style={{ marginTop: ".35rem", display: "flex",
            gap: ".4rem", alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
      <span className="mut" style={{ fontSize: 12, flexBasis: "100%" }}>
        Enter the total and date</span>
      <input aria-label="total" inputMode="decimal" placeholder="total"
        value={total} onChange={(e) => setTotal(e.target.value)}
        style={{ width: "6.5rem" }} />
      <input aria-label="date" type="date" value={date}
        min={bounds?.min} max={bounds?.max}
        onChange={(e) => setDate(e.target.value)} />
      <input aria-label="store" placeholder="store (optional)" maxLength={80}
        value={store} onChange={(e) => setStore(e.target.value)}
        style={{ width: "11rem" }} />
      <button type="submit" style={{ fontSize: 12 }} disabled={save.isPending}>
        {save.isPending ? "saving…" : "Save"}</button>
      {onDone && <button type="button" style={{ fontSize: 12 }}
        onClick={onDone}>cancel</button>}
      {(err || save.isError) && (
        <span style={{ color: "var(--red)", fontSize: 12, flexBasis: "100%" }}>
          {err ?? errText(save.error)}</span>)}
    </form>
  );
}

export default function WaitingReceipts() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  // the demo takes matches and unmatches but refuses uploads, typed-in
  // details and removals — those controls are not offered there
  const demo = !!me.data?.demo;
  const q = useWaitingReceipts();
  const match = useMatchReceipt();
  const unmatch = useUnmatchReceipt();
  const more = useMoreWaiting();
  const del = useMutation({
    mutationFn: (id: string) => api.receiptDelete(id),
    onSuccess: (_r, id) => {
      removeFromList<Waiting, WaitingReceipt>(qc, WAITING_KEY, "waiting",
        (x) => x.id === id);
      // `count` is every receipt waiting, not the rows shown: one fewer
      qc.setQueryData<Waiting>(WAITING_KEY,
        (d) => d && { ...d, count: Math.max(0, d.count - 1) });
    },
  });
  const parse = useMutation({
    mutationFn: (id: string) => api.receiptParse(id),
    // flip to reading at once — the poll above keys off the cache
    onSuccess: (_r, id) => patchList<Waiting, WaitingReceipt>(
      qc, WAITING_KEY, "waiting", (x) => x.id === id,
      { status: "parsing", error: null }),
  });
  // a receipt whose read total is wrong: its details form, opened by hand
  const [fixing, setFixing] = useState<string | null>(null);
  const d = q.data;
  const busy = (m: { isPending: boolean; variables?: unknown }, v: unknown) =>
    m.isPending && m.variables === v;
  return (
    <div className="card" style={{ marginTop: 0 }} id="waiting">
      <div style={{ display: "flex", gap: ".7rem", alignItems: "center",
                    flexWrap: "wrap" }}>
        <h2 style={{ margin: 0 }}>Waiting receipts
          {d && d.count > 0 && <span className="pill m"
            style={{ marginLeft: 8, fontSize: 12 }}>{d.count}</span>}</h2>
        {mayEdit && !demo && <SnapReceiptButton />}
      </div>
      <p className="mut" style={{ fontSize: 12, margin: ".35rem 0 0" }}>
        Snap a receipt before the charge shows up — it matches its
        transaction by amount, date and store when the charge reaches the
        ledger. When two charges fit equally, you pick.</p>
      {d && !d.ai_available && (
        <p className="mut" style={{ fontSize: 12, margin: ".35rem 0 0" }}>
          This instance has no AI endpoint to read receipts — enter the total
          and date yourself, or add one in{" "}
          <Link to="/settings/ai">Settings → AI</Link>.</p>)}
      {match.isError && <p style={{ color: "var(--red)" }}>{errText(match.error)}</p>}
      {unmatch.isError && <p style={{ color: "var(--red)" }}>{errText(unmatch.error)}</p>}
      {q.isPending && <p className="mut">loading…</p>}
      {d && d.waiting.length === 0 && (
        <p className="mut" style={{ margin: ".5rem 0 0" }}>
          Nothing waiting — every receipt has its transaction.</p>)}
      {(d?.waiting ?? []).map((r) => {
        const st = statusWords(r);
        return (
          <div key={r.id} style={{ display: "flex", gap: ".7rem",
                marginTop: ".6rem", borderTop: "1px solid var(--line)",
                paddingTop: ".5rem", alignItems: "flex-start" }}>
            <Thumb r={r} />
            <div style={{ flex: 1, minWidth: 0 }}>
              <div>
                <b>{r.merchant ?? "Receipt"}</b>
                {r.total != null && <> · {money(r.total, true)}</>}
                {r.date && <span className="mut"> · {mmdd(r.date)}</span>}{" "}
                <span className={"pill " + st.cls}>{st.text}</span>
              </div>
              {r.status === "parsing" && (
                <div style={{ marginTop: ".35rem" }}>
                  <div className="rcpt-progress"><span /></div></div>)}
              {r.status === "failed" && r.error && (
                <div style={{ color: "var(--red)", fontSize: 12,
                              marginTop: ".25rem" }}>{r.error}</div>)}
              {r.status === "uploaded" && !r.needs_details && (
                <div className="mut" style={{ fontSize: 12, marginTop: ".2rem" }}>
                  Queued to be read.</div>)}
              {r.manual && (
                <div className="mut" style={{ fontSize: 12, marginTop: ".2rem" }}>
                  Total and date entered by hand.</div>)}
              {mayEdit && !demo && (r.needs_details || fixing === r.id) && (
                <DetailsForm key={r.id + (r.total ?? "")} r={r}
                  onDone={fixing === r.id ? () => setFixing(null) : undefined} />)}
              {r.stale && (
                <div className="mut" style={{ fontSize: 12, marginTop: ".2rem" }}>
                  No charge has matched in {d?.stale_days ?? 30} days — match
                  it by hand, or remove it.</div>)}
              {r.tie && (
                <div className="mut" style={{ fontSize: 12, marginTop: ".2rem" }}>
                  More than one charge fits — pick the right one.</div>)}
              {r.released && (
                <div className="mut" style={{ fontSize: 12, marginTop: ".2rem" }}>
                  Was matched by hand — that charge was removed. Pick its
                  charge again.</div>)}
              {r.candidates.length > 0 && (
                <div style={{ marginTop: ".35rem", display: "flex",
                              flexDirection: "column", gap: ".25rem" }}>
                  {r.candidates.map((c) => (
                    <div key={c.txn_id} style={{ display: "flex", gap: ".5rem",
                          alignItems: "center", flexWrap: "wrap", fontSize: 13 }}>
                      <span className="mut" style={{ whiteSpace: "nowrap" }}>
                        {mmdd(c.date)}</span>
                      <span style={{ whiteSpace: "nowrap" }}>
                        {money(c.amount, true)}</span>
                      <span style={{ overflow: "hidden", textOverflow: "ellipsis",
                                     maxWidth: "16rem", whiteSpace: "nowrap" }}>
                        {c.payee}</span>
                      {c.pending && <span className="pill m">pending</span>}
                      {c.exact && <span className="pill g">exact amount</span>}
                      {mayEdit && (
                        <button style={{ fontSize: 12 }}
                          disabled={match.isPending}
                          onClick={() => match.mutate({ id: r.id, txnId: c.txn_id })}>
                          match</button>)}
                    </div>
                  ))}
                </div>)}
            </div>
            {mayEdit && (
              <div style={{ display: "flex", flexDirection: "column", gap: ".25rem" }}>
                {d?.ai_available && r.status !== "parsed"
                  && r.status !== "parsing" && (
                  <button style={{ fontSize: 12 }}
                    disabled={busy(parse, r.id)}
                    onClick={() => parse.mutate(r.id)}>
                    {r.status === "failed" ? "retry" : "read now"}</button>)}
                {!demo && r.status === "parsed" && !r.needs_details
                  && fixing !== r.id && (
                  <button style={{ fontSize: 12 }}
                    title="the total or date was read wrong — type them in"
                    onClick={() => setFixing(r.id)}>fix total</button>)}
                {!demo && (
                  <button style={{ fontSize: 12 }}
                    disabled={busy(del, r.id)}
                    onClick={() => del.mutate(r.id)}>remove</button>)}
              </div>)}
          </div>
        );
      })}
      {moreWaiting(d) && (
        <div style={{ marginTop: ".6rem", borderTop: "1px solid var(--line)",
                      paddingTop: ".5rem", fontSize: 13 }}>
          <span className="mut">
            Showing {d!.waiting.length} of {d!.count}.{" "}</span>
          <button style={{ fontSize: 12 }} disabled={more.isPending}
            onClick={() => more.mutate()}>
            {more.isPending ? "loading…" : "show more"}</button>
          {more.isError && (
            <span style={{ color: "var(--red)", fontSize: 12, marginLeft: 8 }}>
              {errText(more.error)}</span>)}
        </div>)}
      {(d?.recent.length ?? 0) > 0 && (
        <>
          <h3 style={{ margin: ".9rem 0 .2rem", fontSize: 14 }}>Matched recently</h3>
          {d!.recent.map((m) => (
            <div key={m.id} style={{ display: "flex", gap: ".6rem",
                  alignItems: "center", flexWrap: "wrap", fontSize: 13,
                  marginTop: ".3rem" }}>
              <Thumb r={m} />
              <span>{m.payee}</span>
              <span>{money(m.txn_amount, true)}</span>
              <span className="mut">{mmdd(m.txn_date)}</span>
              <span className="pill g">matched automatically</span>
              {mayEdit && (
                <button style={{ fontSize: 12 }}
                  disabled={busy(unmatch, m.id)}
                  onClick={() => unmatch.mutate(m.id)}
                  title="wrong transaction — put the receipt back to waiting">
                  unmatch</button>)}
            </div>
          ))}
        </>
      )}
    </div>
  );
}
