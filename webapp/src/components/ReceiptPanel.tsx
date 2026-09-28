import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, errText, type Receipt, type Txn } from "../api/client";
import { patchList, patchQueries, removeFromList } from "../api/cache";
import { rankWaiting, useMatchReceipt, useUnmatchReceipt, useWaitingReceipts,
         waitingMatches } from "../api/waiting";
import { isViewer } from "../role";

// receipts on a transaction — upload, parsed line items, retry,
// delete. Budget math untouched; this is documentation.
// There are deliberately no auto-tags: the panel shows $ · qty · item
// only — items are searchable from the Transactions search and groupable
// on the Items view instead of tagged.

// house style: MM/DD/YY (parsed dates arrive as YYYY-MM-DD)
const fmtDate = (d: string) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(d);
  return m ? `${m[2]}/${m[3]}/${m[1].slice(2)}` : d;
};

// how many waiting receipts show before "more…"
const OFFER_HEAD = 3;

export default function ReceiptPanel({ txnId }: { txnId: string }) {
  const qc = useQueryClient();
  // upload / parse / remove are owner writes; viewers still view
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const viewer = isViewer(me);
  // the vision parse runs in the background — poll while any
  // receipt is 'parsing' so the progress bar and result appear live
  const q = useQuery({ queryKey: ["receipts", txnId],
                       queryFn: () => api.receipts(txnId),
                       refetchInterval: (query) =>
                         query.state.data?.receipts.some(
                           (r) => r.status === "parsing") ? 1500 : false });
  // the 📎 on the ledger row reads has_receipt from whichever list the row
  // sits in; set it from what this panel now knows, then let the ledger
  // refetch confirm (the list is the one key this write really moves)
  const markRow = (has: boolean) => {
    const fn = (t: Txn) => t.id === txnId ? { ...t, has_receipt: has } : t;
    patchQueries<{ rows: Txn[] }>(qc, ["txns"],
      (d) => d.rows ? { ...d, rows: d.rows.map(fn) } : d);
    patchQueries<{ txns: Txn[] }>(qc, ["bills-history"],
      (d) => d.txns ? { ...d, txns: d.txns.map(fn) } : d);
    qc.invalidateQueries({ queryKey: ["txns"] });
  };
  const upload = useMutation({
    mutationFn: ({ f, kind }: { f: File; kind: "receipt" | "check" }) =>
      api.receiptUpload(txnId, f, kind),
    // the upload answers with the full list — seed the cache with it, so
    // the new receipt (and its progress bar) is on screen now
    onSuccess: (r) => {
      qc.setQueryData<{ receipts: Receipt[];
                        txn?: { amount: number; date: string } | null }>(
        ["receipts", txnId], (d) => ({ ...d, receipts: r.receipts }));
      markRow(true);
    } });
  const del = useMutation({
    mutationFn: (id: string) => api.receiptDelete(id),
    onSuccess: (_r, id) => {
      removeFromList<{ receipts: Receipt[] }, Receipt>(
        qc, ["receipts", txnId], "receipts", (x) => x.id === id);
      const left = qc.getQueryData<{ receipts: Receipt[] }>(["receipts", txnId]);
      markRow(!!left?.receipts.length);
    } });
  const parse = useMutation({
    mutationFn: (id: string) => api.receiptParse(id),
    // flip the row to parsing at once — the poll above reads the cache, so
    // it starts on this frame instead of after a refetch lands
    onSuccess: (_r, id) => {
      patchList<{ receipts: Receipt[] }, Receipt>(
        qc, ["receipts", txnId], "receipts", (x) => x.id === id,
        { status: "parsing", error: null });
      qc.invalidateQueries({ queryKey: ["receipts", txnId] });
    } });
  // receipts snapped before a charge arrived: offered here for a hand match
  // (the matcher could not decide, or nothing was read off the receipt), and
  // a matched one can be put back to waiting
  const waiting = useWaitingReceipts();
  const match = useMatchReceipt();
  const unmatch = useUnmatchReceipt();
  // ranked against THIS charge (amount, then date), so the receipt that
  // fits is offered first however many are waiting; the head shows, and
  // the whole list is one tap away with a search, so any waiting receipt
  // can be paired from here — the server takes any live charge
  const [more, setMore] = useState(false);
  const [find, setFind] = useState("");
  const ranked = rankWaiting(waiting.data?.waiting ?? [],
    q.data?.txn ? { id: txnId, ...q.data.txn } : null);
  const offered = more ? ranked.filter((w) => waitingMatches(w, find))
    : ranked.slice(0, OFFER_HEAD);
  return (
    <div className="card" style={{ margin: ".3rem 0 .6rem" }}>
      <div style={{ display: "flex", gap: ".7rem", alignItems: "center",
                    flexWrap: "wrap" }}>
        <b>Receipts</b>
        {!viewer && (<>
        <label className="btn" style={{ cursor: "pointer" }}>
          {upload.isPending ? "uploading…" : "attach image / PDF"}
          <input type="file" accept="image/*,.pdf"
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) upload.mutate({ f, kind: "receipt" });
              e.target.value = "";
            }} />
        </label>
        {/* explicit beats magic: the user says it's a check, and the parse
            reads check fields (payee/memo/number) instead of line items */}
        <label className="btn" style={{ cursor: "pointer" }}
               title="photo of the front of a written check — parses payee, amount, memo, check number">
          attach check image
          <input type="file" accept="image/*,.pdf"
            style={{ display: "none" }}
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) upload.mutate({ f, kind: "check" });
              e.target.value = "";
            }} />
        </label>
        </>)}
        <span className="mut" style={{ fontSize: 12 }}>receipts parse
          into line items when an LLM is configured (Settings) —
          items then match the search above and group on the{" "}
          <Link to="/items">Receipts page</Link>. Check images parse into
          payee, amount, and memo instead.</span>
      </div>
      {upload.isError && <p style={{ color: "var(--red)" }}>{errText(upload.error)}</p>}
      {!viewer && ranked.length > 0 && (
        <div style={{ marginTop: ".5rem", fontSize: 13, display: "flex",
                      gap: ".4rem", alignItems: "center", flexWrap: "wrap" }}>
          <span className="mut">snapped earlier and still waiting:</span>
          {offered.map((w) => (
            <button key={w.id} style={{ fontSize: 12 }}
              disabled={match.isPending}
              title="attach this waiting receipt to this transaction"
              onClick={() => match.mutate({ id: w.id, txnId })}>
              ＋ {w.merchant ?? "receipt"}
              {w.total != null && ` $${w.total.toFixed(2)}`}
              {w.date && ` · ${fmtDate(w.date)}`}</button>
          ))}
          {more && offered.length === 0 && (
            <span className="mut" style={{ fontSize: 12 }}>
              no waiting receipt matches</span>)}
          {!more && ranked.length > OFFER_HEAD && (
            <button style={{ fontSize: 12 }} onClick={() => setMore(true)}>
              more… ({ranked.length - OFFER_HEAD})</button>)}
          {more && (<>
            <input aria-label="search waiting receipts" autoFocus
              placeholder="store, total or date" value={find}
              onChange={(e) => setFind(e.target.value)}
              style={{ fontSize: 12, width: "11rem" }} />
            <button style={{ fontSize: 12 }}
              onClick={() => { setMore(false); setFind(""); }}>fewer</button>
          </>)}
          <Link to="/items" style={{ fontSize: 12 }}>all waiting</Link>
        </div>)}
      {match.isError && <p style={{ color: "var(--red)" }}>{errText(match.error)}</p>}
      {del.isError && <p style={{ color: "var(--red)" }}>{errText(del.error)}</p>}
      {(q.data?.receipts ?? []).map((r) => (
        <div key={r.id} style={{ marginTop: ".6rem", borderTop:
              "1px solid var(--line)", paddingTop: ".5rem" }}>
          {r.has_optimized ? (<>
            {/* both copies, as equal buttons — the cleaned-up scan and
                the untouched photo; as a tiny link the original goes
                unfound */}
            <a className="btn" style={{ fontSize: 12, padding: ".15rem .5rem" }}
               href={`/api/receipts/${r.id}/image?variant=optimized`}
               target="_blank" rel="noreferrer"
               title="auto-cropped, straightened, cleaned-up copy">
              optimized</a>{" "}
            <a className="btn" style={{ fontSize: 12, padding: ".15rem .5rem" }}
               href={`/api/receipts/${r.id}/image`}
               target="_blank" rel="noreferrer"
               title="the untouched upload">
              original</a>
          </>) : (
            <a href={`/api/receipts/${r.id}/image`} target="_blank"
               rel="noreferrer">view {r.mime === "application/pdf"
                 ? "PDF" : "image"}</a>
          )}{" "}
          <span className={"pill " + (r.status === "parsed" ? "g"
            : r.status === "failed" ? "r" : "m")}>{r.status}</span>
          {r.kind === "check" && <span className="pill m"
            style={{ marginLeft: 6 }}>check</span>}
          {r.match_method && <span className="pill g" style={{ marginLeft: 6 }}
            title="this receipt was snapped before the charge arrived">
            {r.match_method === "auto" ? "matched automatically"
              : "matched by hand"}</span>}
          {!viewer && r.match_method && (
            <button style={{ marginLeft: 8, fontSize: 12 }}
              disabled={unmatch.isPending && unmatch.variables === r.id}
              title="wrong transaction — put the receipt back to waiting"
              onClick={() => unmatch.mutate(r.id, {
                onSuccess: () => {
                  removeFromList<{ receipts: Receipt[] }, Receipt>(
                    qc, ["receipts", txnId], "receipts", (x) => x.id === r.id);
                  const left = qc.getQueryData<{ receipts: Receipt[] }>(
                    ["receipts", txnId]);
                  markRow(!!left?.receipts.length);
                } })}>unmatch</button>)}
          {r.parsed?.merchant && <span className="mut">
            {" "}{r.parsed.merchant}{r.parsed.total != null &&
              ` · $${r.parsed.total}`}</span>}
          {!viewer && r.status !== "parsed" && r.status !== "parsing" && (
            <button style={{ marginLeft: 8, fontSize: 12 }}
              disabled={parse.isPending && parse.variables === r.id}
              onClick={() => parse.mutate(r.id)}>
              {r.status === "failed" ? "retry parse" : "parse now"}</button>
          )}
          {!viewer && (
          <button style={{ marginLeft: 8, fontSize: 12 }}
            disabled={del.isPending && del.variables === r.id}
            onClick={() => del.mutate(r.id)}>remove</button>
          )}
          {r.status === "failed" && r.error && (
            <div style={{ color: "var(--red)", fontSize: 12,
                          marginTop: ".25rem" }}>{r.error}</div>)}
          {r.status === "parsing" && (
            <div style={{ marginTop: ".35rem" }}>
              <div className="rcpt-progress"><span /></div>
              <div className="mut" style={{ fontSize: 12,
                    marginTop: ".15rem" }}>
                reading the receipt with the vision model — up to a minute
                or two on CPU. This updates by itself, and it's safe to
                navigate away: parsing finishes on the server, and the
                line items will be here when you come back. Queued LLM
                work shows under Settings → System health.</div>
            </div>)}
          {r.kind === "check" && r.status === "parsed" && r.parsed && (
            <div style={{ fontSize: 13, marginTop: ".35rem" }}>
              {r.parsed.check_number && <>check #{r.parsed.check_number} · </>}
              {r.parsed.payee && <b>{r.parsed.payee}</b>}
              {r.parsed.amount != null &&
                <> · ${r.parsed.amount.toFixed(2)}</>}
              {r.parsed.date && <> · {fmtDate(r.parsed.date)}</>}
              {r.parsed.memo && <> · memo: {r.parsed.memo}</>}
              {r.parsed.bank && <span className="mut"> · {r.parsed.bank}</span>}
              {r.amount_mismatch && (
                <div className="mut" style={{ fontSize: 12,
                      marginTop: ".15rem" }}>
                  check amount differs from this transaction's amount —
                  the transaction is unchanged</div>)}
            </div>)}
          {r.items.length > 0 && (
            <table style={{ fontSize: 13, marginTop: ".4rem" }}>
              <tbody>
                {r.items.map((it) => (
                  <tr key={it.line}>
                    <td className="num">${it.amount.toFixed(2)}</td>
                    <td className="num">{it.qty ?? ""}</td>
                    <td>{it.description}
                      {it.tag ? <span className="pill m"
                          style={{ marginLeft: 6 }}>{it.tag}</span> : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      ))}
    </div>
  );
}
