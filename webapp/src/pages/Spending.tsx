// Spending — the Spending TAB of Cash Flow: one timeframe (the site-wide
// 3m/6m/1y/3y/5y/all toggle), the conclusion in a sentence, what CHANGED
// vs the previous equal window, then where it went and to whom — ranked
// bars with share and pace instead of tables. The archival tables (spend
// by year, category × year matrix, Amazon) live behind "Records" and are
// fetched only when opened. Mobile twin: mobile/src/app/spending.tsx —
// keep them in step.
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, catLabel, money, type SpendingReport, type SpendingWindow }
  from "../api/client";
import AreaChart from "../components/AreaChart";
import MerchantAvatar from "../components/MerchantAvatar";
import { RangePicker, type Range } from "../components/RangePicker";

// how the story names the window being compared against
export const PREV_NAME: Record<Range, string> = {
  cur: "month before", "1m": "month before that",
  "3m": "3 months before", "6m": "6 months before",
  "1y": "year before", "3y": "3 years before", "5y": "5 years before",
  all: "",
};

/** "+$2,410 (+12%)", colored by whether the move is good. Spend going up
 *  is bad; income going up is good — the caller says which way is which. */
export function Delta({ cur, prev, downIsGood }: {
  cur: number; prev: number | null; downIsGood: boolean;
}) {
  if (prev === null) return null;
  const d = cur - prev;
  if (Math.abs(d) < 0.005) return <span className="mut">unchanged</span>;
  const good = downIsGood ? d < 0 : d > 0;
  const sign = d >= 0 ? "+" : "−";
  const p = prev ? Math.round((100 * d) / prev) : null;
  return (
    <span className={good ? "pos" : "neg"} style={{ fontWeight: 600 }}>
      {sign}{money(Math.abs(d))}{p !== null && Math.abs(p) < 1000
        ? ` (${sign}${Math.abs(p)}%)` : ""}
    </span>
  );
}

const share = (v: number, total: number) =>
  total ? `${Math.round((100 * v) / total)}%` : "";

export default function SpendingSection({ range, setRange }: {
  range: Range; setRange: (v: Range) => void;
}) {
  const wq = useQuery({ queryKey: ["report", "spending-window", range],
                        queryFn: () => api.reportSpendingWindow(range),
                        placeholderData: (prev) => prev });
  // Every range but "This month" holds complete months only, so a ledger
  // that begins this month reads empty on the default 1y until the month
  // turns. Only an empty 1y asks whether this month has spend, so the
  // card below can tell "nothing yet" from "nothing closed yet". A server
  // that already fell back to the month so far says `live_month`, and its
  // window is never this empty case.
  const emptyDefault = !!wq.data && range === "1y" && !wq.data.total
    && !wq.data.by_month.length && !wq.data.live_month;
  const curQ = useQuery({ queryKey: ["report", "spending-window", "cur"],
                          queryFn: () => api.reportSpendingWindow("cur"),
                          enabled: emptyDefault });
  const [allMerch, setAllMerch] = useState(false);
  if (wq.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (wq.isLoadingError) return <div className="note bad">Couldn't load: {String(wq.error)}</div>;
  const w: SpendingWindow = wq.data;

  // a ledger with no spend yet: one honest card instead of empty lists
  if (emptyDefault) {
    // wait for the month-so-far answer rather than flash the wrong copy
    if (curQ.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
    const thisMonth = curQ.data && (curQ.data.total || curQ.data.by_month.length)
      ? curQ.data : null;
    return (
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                      flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Spending</h2>
          <span style={{ marginLeft: "auto" }}>
            <RangePicker value={range} onChange={setRange} />
          </span>
        </div>
        {thisMonth ? (
          <p className="mut">{w.label} holds complete months only, and your
            ledger starts this month — {money(thisMonth.total)} spent so far.{" "}
            <button type="button" className="linklike"
                    style={{ background: "none", border: "none", padding: 0,
                             cursor: "pointer", font: "inherit",
                             color: "var(--ink)", textDecoration: "underline",
                             textDecorationColor: "var(--mut)" }}
                    onClick={() => setRange("cur")}>
              See this month →</button></p>
        ) : (
          <>
            <p className="mut">Nothing to chart yet. Once transactions arrive —
              connect a bank or import a file — this page fills in: what changed,
              where it went, and your top merchants.</p>
            <p><Link to="/accounts">Accounts →</Link>{" · "}
               <Link to="/import">Import →</Link></p>
          </>
        )}
      </div>
    );
  }

  const perMonth = w.total / Math.max(w.months, 1);
  // the server follows the ranked categories with ones spent on only in
  // the previous window, at 0: they belong in "what changed", never in a
  // ranking of where this window's money went
  const spent = w.categories.filter(([, v]) => v > 0);
  const top = spent[0];
  // the movers: biggest absolute changes vs the previous window, either
  // way — a category that fell to nothing included — then merchants paid
  // only in the previous window, in the same row shape
  const moved = (rows: [string, number, number | null][], n: number,
                 label: (k: string) => string) =>
    rows.map(([c, v, p]) => ({ k: c, c: label(c), v, p: p ?? 0,
                               d: v - (p ?? 0) }))
      .filter((m) => Math.abs(m.d) >= Math.max(25, w.total * 0.005))
      .sort((a, b) => Math.abs(b.d) - Math.abs(a.d))
      .slice(0, n);
  const movers = w.prev_total === null ? [] : [
    ...moved(w.categories, 4, catLabel).map((m) => ({ ...m, k: `c:${m.k}` })),
    ...moved((w.dropped_merchants ?? []).map(([payee, v, , p]) =>
               [payee, v, p] as [string, number, number | null]), 3,
             (k) => k).map((m) => ({ ...m, k: `m:${m.k}` })),
  ];
  const merchants = allMerch ? w.merchants : w.merchants.slice(0, 10);

  return (
    <>
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                      flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Spending</h2>
          <span style={{ marginLeft: "auto" }}>
            <RangePicker value={range} onChange={setRange} />
          </span>
        </div>
        <div style={{ display: "flex", gap: ".5rem", alignItems: "baseline",
                      flexWrap: "wrap", marginTop: ".5rem", fontSize: 13 }}>
          {/* live_month: the ledger starts this month, so the range shows
              the month so far — named as such, and a per-month pace over
              one partial month would only repeat the total */}
          <span className="sub">{w.live_month ? `${w.label} so far` : w.label}:</span>
          <b style={{ fontSize: "1.15rem" }}>{money(w.total)}</b> spent
          {!w.live_month && <><span className="mut">·</span>
            <b>{money(perMonth)}</b>/mo</>}
          {w.prev_total !== null && (
            <><span className="mut">·</span>
              <Delta cur={w.total} prev={w.prev_total} downIsGood />
              <span className="sub">vs the {PREV_NAME[range]}</span></>
          )}
          {top && (
            <><span className="mut">·</span>
              <span className="sub">biggest</span> <b>{catLabel(top[0])}</b>
              <span className="sub">{money(top[1])} · {share(top[1], w.total)}</span></>
          )}
        </div>
        {w.by_month.length >= 4 && (
          <div style={{ marginTop: ".6rem" }}>
            <AreaChart points={w.by_month} height={160}
                       xticks={w.months > 14 ? "years" : undefined} />
          </div>
        )}
      </div>

      {movers.length > 0 && (
        <div className="card">
          <h2>What changed <span className="sub" style={{ fontWeight: 400 }}>
            — vs the {PREV_NAME[range]}</span></h2>
          {movers.map((m) => (
            <div key={m.k} style={{ fontSize: 13, margin: ".25rem 0" }}>
              <span className={m.d > 0 ? "neg" : "pos"}
                    style={{ fontWeight: 700 }}>
                {m.d > 0 ? "▲" : "▼"} {m.c}{" "}
                {m.d > 0 ? "+" : "−"}{money(Math.abs(m.d))}
              </span>{" "}
              <span className="sub">
                {money(m.p)} → {money(m.v)}
                {m.p ? ` · ${m.d > 0 ? "+" : "−"}${Math.abs(Math.round((100 * m.d) / m.p))}%` : ""}
              </span>
            </div>
          ))}
        </div>
      )}

      <div className="card">
        <h2>Where it went <span className="sub" style={{ fontWeight: 400 }}>
          — share of the window's spend</span></h2>
        <table>
          <tbody>
            {spent.slice(0, 15).map(([c, v, p]) => (
              <tr key={c}>
                <td style={{ maxWidth: "11rem" }}>{catLabel(c)}</td>
                <td style={{ width: "34%" }}>
                  <span style={{ display: "inline-block", height: 9,
                                 borderRadius: 3, background: "var(--blue, #4f9bd6)",
                                 verticalAlign: "middle",
                                 width: `${Math.max(2, (100 * v) / (spent[0]?.[1] || 1))}%` }} />
                </td>
                <td className="num">{money(v)}</td>
                <td className="num mut" style={{ fontSize: 12 }}>{share(v, w.total)}</td>
                <td className="num mut" style={{ fontSize: 12 }}>
                  {money(v / Math.max(w.months, 1))}/mo</td>
                <td className="num" style={{ fontSize: 12 }}>
                  <Delta cur={v} prev={p} downIsGood /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>Top merchants</h2>
        <table>
          <tbody>
            {merchants.map(([payee, amt, n, p, logo]) => (
              <tr key={payee}>
                <td>
                  <MerchantAvatar name={payee} logo={logo} style={{ marginRight: 7 }} />
                  <Link to={`/bills/history?payee=${encodeURIComponent(payee)}`}
                        style={{ textDecoration: "none", color: "inherit" }}>{payee}</Link>
                </td>
                <td className="num">{money(amt)}</td>
                <td className="num mut">{n}×</td>
                <td className="num" style={{ fontSize: 12 }}>
                  <Delta cur={amt} prev={p} downIsGood /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {w.merchants.length > 10 && (
          <p className="sub" style={{ margin: ".4rem 0 0" }}>
            <a onClick={() => setAllMerch(!allMerch)} style={{ cursor: "pointer" }}>
              {allMerch ? "top 10 only" : `all ${w.merchants.length} →`}</a>
          </p>
        )}
      </div>

      <Records />
    </>
  );
}

/** The archival tables — collapsed, and the full spending report is
 *  only fetched once this is opened. */
function Records() {
  const [open, setOpen] = useState(false);
  const q = useQuery({ queryKey: ["report", "spending"],
                       queryFn: api.reportSpending, enabled: open });
  const sp: SpendingReport | undefined = q.data;
  const yearsRev = sp ? [...sp.yoy_years].reverse() : [];
  return (
    <details onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}
             open={open} style={{ margin: ".8rem 0" }}>
      <summary className="mut" style={{ cursor: "pointer", fontSize: 13 }}>
        Records — spend by year · category × year matrix · Amazon by category
      </summary>
      {open && q.isPending && <p className="mut">loading…</p>}
      {sp && (
        <>
          <div className="grid cols2">
            <div className="card"><h2>Spend by year</h2>
              <table>
                <thead><tr><th>Year</th><th className="num">Spent</th>
                  <th className="num">Txns</th></tr></thead>
                <tbody>
                  {[...sp.by_year].reverse().map(([yr, amt, n]) => (
                    <tr key={yr}><td>{yr}</td>
                      <td className="num">{money(amt)}</td>
                      <td className="num">{n}</td></tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="card"><h2>Amazon by category</h2>
              <table><tbody>
                {sp.amazon.map(([cat, amt, n]) => (
                  <tr key={cat}><td>{cat}</td>
                    <td className="num">{money(amt)}</td>
                    <td className="num mut">{n}</td></tr>
                ))}
                {sp.amazon.length === 0 && (
                  <tr><td className="mut">No Amazon-categorized spend.</td></tr>
                )}
              </tbody></table>
            </div>
          </div>
          <div className="card"><h2>Category × year (YoY)</h2>
            <div style={{ overflowX: "auto" }}>
              <table>
                <thead><tr><th>Category</th>
                  {yearsRev.map((y) => <th key={y} className="num">{y}</th>)}</tr></thead>
                <tbody>
                  {sp.yoy_matrix.map((row) => (
                    <tr key={String(row[0])}>
                      <td>{row[0]}</td>
                      {(row.slice(1) as number[]).reverse().map((v, i) => (
                        <td key={i} className="num">{v ? money(v) : "·"}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </>
      )}
    </details>
  );
}
