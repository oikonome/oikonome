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
import { api, catLabel, money, type Change, type SpendingReport,
         type SpendingWindow } from "../api/client";
import { changeLook, EMPTY_LINES } from "../moneymath";
import AreaChart from "../components/AreaChart";
import MerchantAvatar from "../components/MerchantAvatar";
import { RangePicker, rangeCaption, type Range } from "../components/RangePicker";
import PageSkeleton from "../components/PageSkeleton";
import { FailedLine, NoAccountsLine, useNoAccounts } from "../components/Loaded";
import EmptyState from "../components/EmptyState";


/** "+$2,410 (+12%)", colored by whether the move is good — the server's
 *  change (reporting.change: the shown figure minus the shown figure,
 *  "unchanged" at $0). Spend going up is bad; income going up is good —
 *  the caller says which way is which. */
export function Delta({ ch, downIsGood }: {
  ch: Change | null | undefined; downIsGood: boolean;
}) {
  const c = changeLook(ch, downIsGood);
  if (!c) return null;
  if (c.unchanged) return <span className="mut">unchanged</span>;
  return (
    // nowrap: "(+11%)" used to fall onto a second line of its own; in a
    // phone-width table the percentage steps aside instead (index.css
    // .dl-pct) and the dollar change stays on one line
    <span className={c.good ? "pos" : "neg"}
          style={{ fontWeight: 600, whiteSpace: "nowrap" }}>
      {c.sign}{money(c.dollars)}{c.pct !== null
        ? <span className="dl-pct"> ({c.sign}{c.pct}%)</span> : ""}
    </span>
  );
}

// a category's share of the window, as the server decides it
// (reporting.spending_window → category_shares, adding up to 100)
const share = (p: number | null | undefined) => (p == null ? "" : `${p}%`);

/** Where a Spending figure opens: the Transactions page listing exactly
 *  the rows that figure summed, by the server's own predicate (GET
 *  /api/transactions?spend=…, reporting.spending_window_ledger) — a
 *  category, a merchant, or with neither the window's whole spend; the
 *  previous side is the window a dropped merchant was paid in. Mobile
 *  twin: spendDrillParams in mobile/src/lib/pure.ts. */
export function spendDrillHref(range: Range, what: {
  cat?: string; merchant?: string; previous?: boolean;
} = {}): string {
  const p = new URLSearchParams({ spend: range });
  if (what.cat) p.set("cat", what.cat);
  if (what.merchant) p.set("merchant", what.merchant);
  if (what.previous) p.set("side", "previous");
  return `/transactions?${p.toString()}`;
}

const DRILL_LINK = { textDecoration: "none", color: "inherit" } as const;

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
  const noAccounts = useNoAccounts();
  if (wq.isPending) return <PageSkeleton />;
  // a failed range keeps its card and the range picker
  if (wq.data === undefined)
    return (
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                      flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Spending</h2>
          <RangePicker value={range} onChange={setRange}
                       style={{ marginLeft: "auto" }} />
        </div>
        <FailedLine q={wq} what={`spending (${rangeCaption(range)})`} />
      </div>);
  const w: SpendingWindow = wq.data;

  // a ledger with no spend yet: one honest card instead of empty lists
  if (emptyDefault) {
    // wait for the month-so-far answer rather than flash the wrong copy
    if (curQ.isPending) return <PageSkeleton />;
    // an unread month is not "nothing to chart yet": the failed line, with
    // the range picker kept so another window is one tap away
    if (curQ.data === undefined)
      return (
        <div className="card" style={{ marginTop: 0 }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                        flexWrap: "wrap" }}>
            <h2 style={{ margin: 0 }}>Spending</h2>
            <RangePicker value={range} onChange={setRange}
                         style={{ marginLeft: "auto" }} />
          </div>
          <FailedLine q={curQ} what="this month's spending" />
        </div>);
    const thisMonth = curQ.data && (curQ.data.total || curQ.data.by_month.length)
      ? curQ.data : null;
    return (
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                      flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Spending</h2>
          <RangePicker value={range} onChange={setRange}
                       style={{ marginLeft: "auto" }} />
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
        ) : noAccounts ? <NoAccountsLine />
          : <EmptyState title={EMPTY_LINES.spendingRange} />}
      </div>
    );
  }

  // any other window with nothing in it: one line, not "$0 spent" over
  // empty cards (the phone's rule too)
  if (!w.total && !w.by_month.length && !w.live_month)
    return (
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                      flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Spending</h2>
          <RangePicker value={range} onChange={setRange}
                       style={{ marginLeft: "auto" }} />
        </div>
        {noAccounts ? <NoAccountsLine />
          : <EmptyState title={EMPTY_LINES.spendingRange} />}
      </div>);

  // the server follows the ranked categories with ones spent on only in
  // the previous window, at 0: they belong in "what changed", never in a
  // ranking of where this window's money went. Each row carries the
  // server's share, "/mo" and change (spending_window, in row order)
  const spent = w.categories
    .map(([c, v], i) => ({ c, v, sh: w.category_shares[i],
                           pm: w.per_month_by_category[i],
                           ch: w.category_changes[i] }))
    .filter((r) => r.v > 0);
  const top = spent[0];
  // the movers are the server's (spending_window → movers): the largest
  // changes either way, a category that fell to nothing included, then
  // merchants paid only in the previous window
  const movers = w.movers;
  const merchants = (allMerch ? w.merchants : w.merchants.slice(0, 10))
    .map((m, i) => [m, w.merchant_changes[i]] as const);

  return (
    <>
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                      flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>Spending</h2>
          <RangePicker value={range} onChange={setRange}
                       style={{ marginLeft: "auto" }} />
        </div>
        {/* each fact is one item, spaced rather than dotted: when the line
            wraps on a phone a separator would start a row, and a label
            would part from its figure */}
        <div style={{ display: "flex", columnGap: "1rem", rowGap: ".15rem",
                      alignItems: "baseline",
                      flexWrap: "wrap", marginTop: ".5rem", fontSize: 13 }}>
          {/* so_far: the window runs to today — the server's heading says
              "so far", and a per-month pace over one partial month would
              only repeat the total */}
          <span><span className="sub">{w.heading}:</span>{" "}
            <Link to={spendDrillHref(range)} style={DRILL_LINK}
                  title="the charges this total counts">
              <b style={{ fontSize: "1.15rem" }}>{money(w.total)}</b></Link> spent</span>
          {/* per_month_basis: the server's words for what "/mo" divides
              by when that is fewer months than the heading names */}
          {!w.so_far && <span><b>{money(w.per_month)}</b>/mo
            {w.per_month_basis ? ` ${w.per_month_basis}` : ""}</span>}
          {w.change && (
            <span><Delta ch={w.change} downIsGood />{" "}
              <span className="sub">{w.prev_text}</span></span>
          )}
          {top && (
            <span><span className="sub">biggest</span> <b>{catLabel(top.c)}</b>{" "}
              <span className="sub">{money(top.v)} · {share(top.sh)}</span></span>
          )}
        </div>
        {w.dating === "paid" && (
          <p className="sub" style={{ margin: ".25rem 0 0", fontSize: 11 }}>
            dated by the day each charge was paid — the Month and Year
            lenses count a bill in the month it belongs to</p>)}
        {w.by_month.length >= 4 && (
          <div style={{ marginTop: ".6rem" }}>
            <AreaChart points={w.by_month} height={160} subject="Spending by month"
                       xticks={w.months > 14 ? "years" : undefined} />
          </div>
        )}
      </div>

      {movers.length > 0 && (
        <div className="card">
          <h2>What changed <span className="sub" style={{ fontWeight: 400 }}>
            — {w.prev_text}</span></h2>
          {movers.map((m) => {
            const up = m.change.amount > 0;
            const sign = up ? "+" : "−";
            return (
              <div key={`${m.kind}:${m.label}`}
                   style={{ fontSize: 13, margin: ".25rem 0" }}>
                {/* a category opens its charges this window — the window
                    before when it fell to nothing; a merchant here was
                    paid only before, so it opens the window before */}
                <Link to={m.kind === "category"
                            ? spendDrillHref(range, { cat: m.label,
                                                      previous: m.current === 0 })
                            : spendDrillHref(range, { merchant: m.label,
                                                      previous: true })}
                      style={DRILL_LINK}>
                <span className={up ? "neg" : "pos"}
                      style={{ fontWeight: 700 }}>
                  {up ? "▲" : "▼"}{" "}
                  {m.kind === "category" ? catLabel(m.label) : m.label}{" "}
                  {sign}{money(Math.abs(m.change.amount))}
                </span></Link>{" "}
                <span className="sub">
                  {money(m.previous)} → {money(m.current)}
                  {m.change.pct !== null
                    ? ` · ${sign}${Math.abs(m.change.pct)}%` : ""}
                </span>
              </div>
            );
          })}
        </div>
      )}

      <div className="card">
        <h2>Where it went <span className="sub" style={{ fontWeight: 400 }}>
          — share of the window's spend</span></h2>
        <table>
          <tbody>
            {spent.slice(0, 15).map(({ c, v, sh, pm, ch }) => (
              <tr key={c}>
                <td style={{ maxWidth: "11rem" }}>
                  <Link to={spendDrillHref(range, { cat: c })}
                        style={DRILL_LINK}>{catLabel(c)}</Link></td>
                <td className="c-sharebar" style={{ width: "34%" }}>
                  <span style={{ display: "inline-block", height: 9,
                                 borderRadius: 3, background: "var(--blue, #4f9bd6)",
                                 verticalAlign: "middle",
                                 width: `${Math.max(2, (100 * v) / (spent[0]?.v || 1))}%` }} />
                </td>
                <td className="num">
                  <Link to={spendDrillHref(range, { cat: c })}
                        style={DRILL_LINK}>{money(v)}</Link></td>
                <td className="num mut" style={{ fontSize: 12 }}>{share(sh)}</td>
                <td className="num mut hide-m" style={{ fontSize: 12 }}>
                  {money(pm)}/mo</td>
                <td className="num" style={{ fontSize: 12 }}>
                  <Delta ch={ch} downIsGood /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="card">
        <h2>Top merchants</h2>
        <table>
          <tbody>
            {merchants.map(([[payee, amt, n, , logo], ch]) => (
              <tr key={payee}>
                <td>
                  <MerchantAvatar name={payee} logo={logo} style={{ marginRight: 7 }} />
                  {/* the row opens the charges this figure counted; the
                      merchant's whole history — refunds and holds
                      included — is the small link beside it */}
                  {/* a row with no name names no merchant the server can
                      find again, so it opens nothing */}
                  {payee ? <>
                    <Link to={spendDrillHref(range, { merchant: payee })}
                          style={DRILL_LINK}>{payee}</Link>{" "}
                    <Link to={`/bills/history?payee=${encodeURIComponent(payee)}&window=${range}`}
                          className="sub" style={{ fontSize: 11 }}
                          title="every charge filed under this name">history</Link>
                  </> : payee}
                </td>
                <td className="num">
                  {payee ? (
                    <Link to={spendDrillHref(range, { merchant: payee })}
                          style={DRILL_LINK}>{money(amt)}</Link>
                  ) : money(amt)}</td>
                <td className="num mut">{n}×</td>
                <td className="num" style={{ fontSize: 12 }}>
                  <Delta ch={ch} downIsGood /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {w.merchants.length > 10 && (
          <p className="sub" style={{ margin: ".4rem 0 0" }}>
            <button type="button" className="as-link" aria-expanded={allMerch}
                    onClick={() => setAllMerch(!allMerch)} style={{ cursor: "pointer" }}>
              {allMerch ? "top 10 only" : `all ${w.merchants.length} →`}</button>
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
             open={open} className="disclosure" style={{ margin: ".8rem 0" }}>
      <summary className="mut" style={{ fontSize: 13 }}>
        Records — spend by year · category × year matrix · Amazon by category
      </summary>
      {open && q.isPending && <p className="mut">loading…</p>}
      {/* a failed read says so, with a retry, rather than an empty
          disclosure (the phone's line) */}
      {open && !sp && q.isError && (
        <FailedLine q={q} what="the spending records" />)}
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
