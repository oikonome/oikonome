// receipt line items across the whole ledger — searchable like
// transactions, groupable by item / merchant / month ("how much do I
// spend on shampoo"). Read-only: the source of truth is the parsed
// receipts on each transaction.
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import { api, errText, mmdd, money, type ReceiptItemRow } from "../api/client";
import { patchList } from "../api/cache";
import { canEdit } from "../role";

const GROUPS = [
  { id: "item", label: "by item" },
  { id: "merchant", label: "by store" },
  { id: "month", label: "by month" },
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
      <h1>Receipt items</h1>
      <div className="card" style={{ marginTop: 0 }}>
        <form style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                       alignItems: "center" }}
              onSubmit={(e) => { e.preventDefault(); setLive(q); }}>
          <input placeholder="search items… (shampoo, milk)" value={q}
                 size={26} onChange={(e) => setQ(e.target.value)} />
          <button className="pri" type="submit">Search</button>
          {GROUPS.map((g) => (
            <button key={g.id} type="button"
                    className={group === g.id ? "pri" : ""}
                    onClick={() => setGroup(g.id)}>{g.label}</button>
          ))}
          <Link className="btn" to="/transactions"
                style={{ marginLeft: "auto" }}>‹ transactions</Link>
        </form>
        <p className="mut" style={{ fontSize: 12, marginBottom: 0 }}>
          Every line item from parsed receipts. Attach receipts on the
          Transactions page (📎); items also match the Transactions search.
        </p>
      </div>

      {query.isPending && <p className="mut">loading…</p>}
      {d && d.groups.length === 0 && (
        <div className="card"><p className="mut" style={{ margin: 0 }}>
          No line items{live ? ` matching “${live}”` : ""} yet — parsed
          receipts land here.</p></div>
      )}

      {d && d.groups.length > 0 && (
        <div className="card">
          <h2>{GROUPS.find((g) => g.id === group)?.label ?? "grouped"}</h2>
          <table>
            <thead>
              <tr><th>{group === "month" ? "Month"
                    : group === "merchant" ? "Store" : "Item"}</th>
                  <th className="num">Times</th>
                  <th className="num">Total</th>
                  <th className="hide-m">Last bought</th></tr>
            </thead>
            <tbody>
              {d.groups.map((g) => (
                <tr key={g.key}>
                  <td>{g.key}</td>
                  <td className="num">{g.n}</td>
                  <td className="num">{money(g.total)}</td>
                  <td className="mut hide-m">{g.last_date ? mmdd(g.last_date) : ""}</td>
                </tr>
              ))}
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
                  <th className="num hide-m" style={{ width: "1%" }}>Qty</th>
                  <th>Item</th>
                  <th className="hide-m">Store</th></tr>
            </thead>
            <tbody>
              {d.rows.map((r) => (
                <tr key={`${r.receipt_id}-${r.line}`}>
                  <td className="mut" style={{ whiteSpace: "nowrap" }}>{mmdd(r.date)}</td>
                  <td className="num" style={{ whiteSpace: "nowrap" }}>{money(r.amount)}</td>
                  <td className="num hide-m">{r.qty ?? ""}</td>
                  <td>{r.description}
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
                      textOverflow: "ellipsis" }}>{r.payee}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
