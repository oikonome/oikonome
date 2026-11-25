// receipts across the whole ledger: the ones snapped before their
// transaction arrived (waiting to be matched), then every parsed line
// item — searchable like transactions, groupable by item / merchant /
// month ("how much do I spend on shampoo"). The line items are read-only
// here: their source of truth is the parsed receipts on each transaction.
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { UNKNOWN_STORE, api, errText, mmdd, money, type ReceiptItemRow }
  from "../api/client";
import { patchList } from "../api/cache";
import { canEdit } from "../role";
import WaitingReceipts from "../components/WaitingReceipts";
import EmptyState from "../components/EmptyState";
import { EMPTY_LINES } from "../moneymath";
import { FailedLine, StaleLine } from "../components/Loaded";
import SegmentedControl from "../components/SegmentedControl";

const GROUPS = [
  { id: "item", label: "By item" },
  { id: "merchant", label: "By store" },
  { id: "month", label: "By month" },
] as const;

export default function Items() {
  const [q, setQ] = useState("");
  const [live, setLive] = useState("");
  const [group, setGroup] = useState<string>("item");
  const qc = useQueryClient();
  // tagging a line item is an editing write — viewers keep the read view
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  const query = useQuery({
    queryKey: ["receipt-items", live, group],
    queryFn: () => api.receiptItems(live, group),
    placeholderData: (prev) => prev,
  });
  const d = query.data;
  return (
    <>
      <h1>Receipts</h1>
      <WaitingReceipts />
      <div className="card">
        <form style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                       alignItems: "center" }}
              onSubmit={(e) => { e.preventDefault(); setLive(q); }}>
          <input aria-label="Search items" placeholder="search items… (shampoo, milk)" value={q}
                 size={26} onChange={(e) => setQ(e.target.value)} />
          <button className="pri" type="submit">Search</button>
          <SegmentedControl label="Group items" value={group}
            onChange={setGroup}
            options={GROUPS.map((g) => ({ value: g.id, label: g.label }))} />
          <Link className="btn" to="/transactions"
                style={{ marginLeft: "auto" }}>‹ transactions</Link>
        </form>
        <p className="mut" style={{ fontSize: 12, marginBottom: 0 }}>
          Every line item from parsed receipts. Snap a receipt above, or
          attach one on the Transactions page (📎); items also match the
          Transactions search.
        </p>
      </div>

      {query.isPending && <p className="mut">loading…</p>}
      {/* a failed search says so under the form, which stays */}
      <StaleLine query={query} />
      {d === undefined && query.isError && (
        <FailedLine q={query} what="line items" />)}
      {d && d.groups.length === 0 && (
        <div className="card">
          {/* a search that matches nothing says so, with no action (the
              phone's words) */}
          {live ? <EmptyState title={EMPTY_LINES.filtered} />
            : <EmptyState title={EMPTY_LINES.lineItems} to="/transactions"
                          action={EMPTY_LINES.lineItemsAction} />}
        </div>
      )}

      {d && d.groups.length > 0 && (
        <div className="card">
          <h2>{GROUPS.find((g) => g.id === group)?.label ?? "grouped"}</h2>
          <table>
            <thead>
              <tr><th>{group === "month" ? "Month"
                    : group === "merchant" ? "Store" : "Item"}</th>
                  {/* how many were bought (quantity), and on how many
                      receipts (receipts.search_items) */}
                  <th className="num">bought</th>
                  <th className="num">receipts</th>
                  <th className="num">Total</th>
                  <th className="hide-m">Last bought</th></tr>
            </thead>
            <tbody>
              {d.groups.map((g) => (
                <tr key={g.key ?? "\u0000unknown"}>
                  <td>{g.key ?? UNKNOWN_STORE}</td>
                  <td className="num">{g.bought}</td>
                  <td className="num">{g.receipts}</td>
                  <td className="num">{money(g.total)}</td>
                  <td className="mut hide-m">{g.last_date ? mmdd(g.last_date) : ""}</td>
                </tr>
              ))}
              {/* a total that may hold a purchase twice says so, in the
                  server's words (specs/receipts.md) */}
              {d.groups.some((g) => g.note) && (
                <tr><td colSpan={5} className="mut" style={{ fontSize: 12 }}>
                  {d.groups.filter((g) => g.note).map((g) => (
                    <div key={g.key ?? "\u0000unknown"}>
                      {g.key ?? UNKNOWN_STORE}: {g.note}</div>))}
                </td></tr>)}
            </tbody>
          </table>
        </div>
      )}

      {d && d.rows.length > 0 && (
        <div className="card">
          <h2>Items <span className="mut" style={{ fontSize: 13,
              fontWeight: 400 }}>newest first{d.truncated
                ? " · showing the first 300 — narrow the search" : ""}</span></h2>
          <table>
            <thead>
              <tr><th style={{ width: "1%", whiteSpace: "nowrap" }}>Date</th>
                  <th className="num" style={{ width: "1%" }}>$</th>
                  <th>Item</th>
                  <th className="hide-m">Store</th></tr>
            </thead>
            <tbody>
              {d.rows.map((r) => (
                <tr key={`${r.receipt_id}-${r.line}`}
                    style={r.counts === false ? { opacity: .55 } : undefined}>
                  <td className="mut" style={{ whiteSpace: "nowrap" }}>{mmdd(r.date)}</td>
                  {/* the line's total, in cents (the phone's figure); the
                      quantity rides with the item at every width */}
                  <td className="num" style={{ whiteSpace: "nowrap" }}>{money(r.amount, true)}</td>
                  <td>{r.description}
                    {r.qty != null && r.qty !== 1 &&
                      <span className="mut"> ×{r.qty}</span>}
                    {/* listed, never added up, or possibly counted twice:
                        the server's words (specs/receipts.md) */}
                    {r.note && (
                      <span className={r.suspected ? undefined : "mut"}
                            style={{ fontSize: 12,
                                     color: r.suspected ? "var(--amber)"
                                                        : undefined }}>
                        {" "}· {r.note}
                      </span>)}
                    {/* tap to tag — the tag drives the Business itemized
                        report */}
                    {mayEdit && <button className="pill m linklike"
                        style={{ marginLeft: 6, cursor: "pointer",
                                 border: "none" }}
                        title={r.tag ? "edit the tag (empty removes it)"
                                     : "tag this line item"}
                        onClick={() => {
                          const t = window.prompt(
                            `Tag for “${r.description}” (empty removes)`,
                            r.tag ?? "");
                          if (t === null) return;
                          const tag = t.trim();
                          api.receiptTag(r.receipt_id, r.line, tag)
                            .then(() => {
                              // the tag is the whole change: write it into
                              // this row instead of refetching the search,
                              // and refresh the views that aggregate tags
                              patchList<{ rows: ReceiptItemRow[] }, ReceiptItemRow>(
                                qc, ["receipt-items", live, group], "rows",
                                (x) => x.receipt_id === r.receipt_id && x.line === r.line,
                                { tag });
                              qc.invalidateQueries({ queryKey: ["receipt-report"] });
                              qc.invalidateQueries({ queryKey: ["receipts"] });
                            })
                            .catch((e) => window.dispatchEvent(
                              new CustomEvent("oiko-toast",
                                { detail: `Couldn't save the tag: ${errText(e)}` })));
                        }}>
                      {r.tag || "＋ tag"}
                    </button>}
                    {/* a viewer still SEES the tag — only editing is gated */}
                    {!mayEdit && r.tag && <span className="pill m"
                        style={{ marginLeft: 6 }}>{r.tag}</span>}</td>
                  <td className="mut hide-m" style={{ whiteSpace: "nowrap",
                      maxWidth: "14rem", overflow: "hidden",
                      textOverflow: "ellipsis" }}>{r.payee ?? UNKNOWN_STORE}
                    {r.txn_id == null && " · waiting"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
