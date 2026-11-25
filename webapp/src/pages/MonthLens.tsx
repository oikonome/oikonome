// Month lens: the report card — FINAL verdict for past months,
// in-progress + pace projection for the current one. Bucket bars, bills
// paid vs planned, by-category totals, biggest transactions, income vs
// spend. Data: /api/lens/month (web/lenses.py) — the future monthly
// email's compose.
import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";
import MoneyMap, { Track } from "../components/MoneyMap";
import { sentPlanRows, sentPlanTotal } from "../components/PlanBars";
import { api, mmdd, money, type MonthLensData, catLabel } from "../api/client";
import { VERDICT } from "./Today";
import PageSkeleton from "../components/PageSkeleton";
import { FailedLine, NoAccountsLine, StaleLine, useNoAccounts }
  from "../components/Loaded";
import { EMPTY_LINES, signedMoney } from "../moneymath";

// the class for a line whose tone the server chose (lenses.hero_words)
const TONE_CLS: Record<string, string> = { neg: "neg", pos: "pos" };

export default function MonthLens({ y, m }: { y: number; m: number }) {
  const q = useQuery({ queryKey: ["lens-month", y, m],
                       queryFn: () => api.lensMonth(y, m),
                       // a step to another period keeps this one on screen,
                       // dimmed, until it answers (the phone's lens rule)
                       placeholderData: (prev) => prev });
  const noAccounts = useNoAccounts();
  if (q.isPending) return <PageSkeleton />;
  if (q.data === undefined) return <FailedLine q={q} what="the month" />;
  const d: MonthLensData = q.data;
  // no budget the household saved stands behind the month: no verdict,
  // the Today page's no-budget rule (mobile: month.tsx unjudged)
  const unjudged = d.verdict === null;
  // a closed month from before the household had a budget (or one with no
  // data): the server says which (lenses.month_standing) and writes the
  // pill and the sentence; such a month has no budget figures, no money
  // map and no bill schedule to draw
  const past = d.standing === "before_budget" || d.standing === "no_data";
  // a month with no rows at all: its line, and nothing else to draw
  const noData = d.standing === "no_data";
  const v = VERDICT[d.verdict as keyof typeof VERDICT] ?? VERDICT["ON BUDGET"];
  // an unjudged month (verdict null, budget_set false) still carries a
  // numeric variable_budget; the ?? only covers an older server's null
  const budget = d.variable_budget ?? 0;
  // the spendable left and the month's net are the server's
  // (lenses.month_summary → remaining, net)
  const remaining = d.remaining;
  // without the server's left-of figure (an older server) the hero prints
  // the spend alone, as on the phone — never a figure worked out here
  const spendOnly = unjudged || remaining == null;
  const p = d.projection;
  const net = d.net;

  return (
    <div style={q.isPlaceholderData ? { opacity: 0.5, pointerEvents: "none" } : undefined}>
      {/* a failed refetch says the month below is from before it (the
          phone's line) */}
      <StaleLine query={q} />
      {/* a new household: the way to fill the month, above its zeros */}
      {noAccounts && <div className="card" style={{ marginTop: "1rem" }}>
        <NoAccountsLine /></div>}
      {/* ---- hero card, mirroring Today: verdict,
           the big remaining number, the pace bar; income vs spend
           demoted to one flow line ---- */}
      <div className="card" style={{ marginTop: "1rem" }}>
        <div style={{ display: "flex", alignItems: "center", gap: ".5rem" }}>
          {unjudged ? <span className="pill m">{d.standing_label ?? "no budget set"}</span>
            : <span className={`pill ${v.cls}`}>{v.label}</span>}
          <span className="pill m">
            {d.status === "final" ? "final"
              : d.status === "future" ? "planned" : `day ${new Date(d.as_of + "T00:00").getDate()} of ${d.days_in_month}`}
          </span>
        </div>
        {past ? null : spendOnly ? (
          <div className="hero-remaining" style={{ marginTop: ".45rem" }}>
            <b>{money(d.variable_actual)}</b>
            <span className="sub" style={{ fontSize: "1rem", fontWeight: 400 }}>
              {" "}variable spend
            </span>
          </div>
        ) : (<>
        <div className="hero-remaining" style={{ marginTop: ".45rem" }}>
          <b className={remaining < 0 ? "neg" : ""}>{money(Math.abs(remaining))}</b>
          {/* the server's word (lenses.hero_words): over / left of /
              left unspent of */}
          {d.remaining_word && (
          <span className="sub" style={{ fontSize: "1rem", fontWeight: 400 }}>
            {" "}{d.remaining_word}{" "}{money(budget)} to spend
          </span>)}
        </div>
        {/* the tick stands where the server says the pace does; its label
            is the server's too (none on a closed month) */}
        {d.pace_expected != null && (
        <Track paceLabel={d.pace_label} row={{ label: "", judged: true, actual: d.variable_actual,
                                expected: d.pace_expected,
                                budget }} />)}
        </>)}
        {unjudged ? (
          <div className="sub" style={{ marginTop: past ? ".6rem" : "1.4rem" }}>
            {d.standing_text ?? `No budget saved yet, so this month gets no
            verdict — set one on the Budget page.`}
          </div>
        ) : (
        <div className="sub" style={{ marginTop: "1.4rem" }}>
          {/* the verdict line in the server's words and tone; "so far"
              is in them only while the month is under way */}
          {d.variance_text && <b className={TONE_CLS[d.variance_text.tone] ?? ""}
              style={d.variance_text.tone === "warn" ? { color: "var(--amber)" } : undefined}>
            {d.variance_text.text}</b>}
        </div>
        )}
        {/* a closed month with no frozen snapshot is judged against
            TODAY's budget — say so rather than pass it off as history */}
        {!unjudged && d.status === "final" && d.budget_source === "live" && (
          <div className="sub" style={{ marginTop: ".5rem" }}>
            No budget snapshot exists for this month — the verdict compares
            it against the current budget, applied retroactively.
          </div>
        )}
        {p && (
          <div className="sub" style={{ marginTop: ".35rem" }}>
            At the current rate, projected <b>{money(p.pace_total)}</b> by month end
            {/* the projection's last words are the server's */}
            {p.tail && <>{" — "}<b className={TONE_CLS[p.tail_tone ?? ""] ?? ""}
                style={p.tail_tone === "warn" ? { color: "var(--amber)" } : undefined}>
              {p.tail}</b></>}.
          </div>
        )}
        {d.variable_scale && (
          <div className="sub" style={{ marginTop: ".35rem", color: "var(--amber)" }}>
            {/* the one sentence Today, Budget and the phone print */}
            Variable budgets auto-scaled from{" "}
            {money(d.variable_scale.static_total)} to fit this month's bills.</div>
        )}
        {/* income vs spend as one flow line (mirrors Today's plan line) */}
        {!noData && <div style={{ marginTop: ".9rem", paddingTop: ".7rem",
                      borderTop: "1px solid var(--line)", fontSize: 13,
                      display: "flex", gap: ".55rem", flexWrap: "wrap",
                      alignItems: "baseline" }}>
          {/* income the bank feed does not measure: the server's words
              in place of a figure (lenses.month_summary → income_text) */}
          {d.income_text
            ? <span className="mut">{d.income_text}</span>
            : <span className="sub"><b style={{ color: "var(--ink)" }}>{money(d.income.actual)}</b> in
            {/* the server's words for the plan's income, none when they
                would repeat the figure (lenses.income_plan_text) */}
            {d.income.plan_text ? ` (${d.income.plan_text})` : ""}</span>}
          <span className="mut" aria-hidden="true">→</span>
          <span className="sub"><b style={{ color: "var(--ink)" }}>{money(d.spend_total)}</b> spent</span>
          {!d.income_text && <>
          <span className="mut" aria-hidden="true">→</span>
          <span className="sub">net <b className={net !== null ? (net >= 0 ? "pos" : "neg") : ""}>
            {net !== null ? signedMoney(net, money, true) : money(null)}</b></span></>}
        </div>}
        {d.dating === "bill_month" && !past && (
          <div className="sub" style={{ marginTop: ".25rem", fontSize: 11 }}>
            a bill counts in the month it belongs to — Cash Flow dates
            spending by the day it was paid</div>)}
      </div>

      {/* ---- month money map — its headline lives in the hero card ---- */}
      {!past && <div className="card">
        <h2>Monthly budget</h2>
        {/* the server's rows (lenses.month_summary → plan_rows, the
            rows Today sends), its words and Variable spending row */}
        <MoneyMap rows={sentPlanRows(d.plan_rows)}
                  total={sentPlanTotal(d.plan_total)} />
      </div>}

      {/* ---- bills paid vs planned ---- */}
      <div className="grid2">
        {!past && <div className="card">
          <h2>Bills — paid vs planned</h2>
          {/* the due-dated bills (`bills_*`, the Bills row's own); the
              pools are the set-aside line below. older-server path: stands
              in for `bills_posted` / `bills_planned` / `bills_awaiting` —
              the card's combined figures, as it always printed them */}
          <div style={{ display: "flex", gap: "1.6rem", flexWrap: "wrap" }}>
            <div><div className="kpi sm">
                {money(d.bills.bills_posted ?? d.bills.posted)}</div>
              {/* the charges the figure holds, as the server counts them */}
              <div className="sub">posted ({d.bills.bills_posted_counts
                ?? d.bills.posted_counts})</div></div>
            <div><div className="kpi sm">
                {money(d.bills.bills_planned ?? d.bills.planned)}</div>
              <div className="sub">planned</div></div>
            {(d.bills.bills_awaiting ?? d.bills.awaiting) > 0 && (
              <div><div className="kpi sm" style={{ color: "var(--amber)" }}>
                  {money(d.bills.bills_awaiting ?? d.bills.awaiting)}</div>
                <div className="sub">awaiting</div></div>
            )}
          </div>
          {/* the plan's share of the pools, said apart from the bills plan,
              and the plan the two make together (lenses.month_summary).
              older-server path: stands in for `set_aside` — nothing, and
              "planned" above is the whole plan */}
          {d.bills.set_aside != null && (d.bills.envelopes_set_aside ?? 0) > 0 && (
            <div className="sub" style={{ marginTop: ".4rem" }}>
              Envelopes set aside {money(d.bills.envelopes_set_aside ?? 0)} ·{" "}
              {money(d.bills.set_aside)} planned in all</div>)}
          {/* a closed month's own lines, as the server wrote them
              (lenses.closed_bill_words): under plan, each bill not paid */}
          {(d.bills.notes ?? []).length > 0 && (
            <div className="sub" style={{ marginTop: ".4rem" }}>
              {(d.bills.notes ?? []).join(" · ")}</div>
          )}
          {d.bills.overdue.length > 0 && (
            <div className="sub" style={{ marginTop: ".4rem" }}>
              Scheduled but not posted:{" "}
              {d.bills.overdue.slice(0, 5).map(([pp, a, due, gone], i) => (
                // a charge the bank feed withdrew, rather than one never made
                <span key={i}>{pp} {money(a)} {gone
                    ? <span style={{ color: "var(--amber)" }}>(withdrawn by the bank feed {mmdd(gone)})</span>
                    : <>(due {mmdd(due)})</>}
                  {i < Math.min(d.bills.overdue.length, 5) - 1 ? ", " : ""}</span>
              ))}
            </div>
          )}
          {d.bills.paid.length > 0 && (
            <table style={{ marginTop: ".6rem" }}><tbody>
              {d.bills.paid.map((r, i) => (
                <tr key={i}>
                  <td className="mut" style={{ whiteSpace: "nowrap", width: "1%" }}>{mmdd(r.date)}</td>
                  <td>{r.payee} <span className="pill g" style={{ marginLeft: 4 }}>paid ✓</span></td>
                  <td className="num" style={{ whiteSpace: "nowrap" }}>{money(r.amount)}</td>
                </tr>
              ))}
            </tbody></table>
          )}
          {d.bills.envelopes.length > 0 && (
            <div className="sub" style={{ marginTop: ".4rem" }}>
              Envelopes: {d.bills.envelopes.map((e, i) => (
                <span key={i}>{e.payee}{" "}
                  {/* the pool's line as the server prints it: its period's
                      spend, the pool, and the spill counted in spending
                      over the same period */}
                  {e.pair_text != null ? e.pair_text : <>
                    {/* older-server path: stands in for `pair_text` —
                        the pair it always sent */}
                    {e.pair_spent != null
                      ? `${money(e.pair_spent)} of ${money(e.pair_of ?? 0)}`
                      : <>{money(e.shown_used)} of {money(e.shown_of)}</>}
                    {e.shown_period ? ` ${e.shown_period}` : ""}
                    {(e.pair_past ?? e.overflow) > 0
                      ? ` · ${money(e.pair_past ?? e.overflow)} past the pool, counted in spending`
                      : ""}</>}
                  {i < d.bills.envelopes.length - 1 ? " · " : ""}</span>
              ))}
            </div>
          )}
          {/* the phone's rule: nothing paid, due, planned or posted, no
              envelope and none of the server's lines about the bills */}
          {d.bills.paid.length === 0 && d.bills.overdue.length === 0 &&
            d.bills.envelopes.length === 0 && !(d.bills.posted > 0) &&
            !(d.bills.planned > 0) && !(d.bills.awaiting > 0) &&
            (d.bills.notes ?? []).length === 0 && (
            <p className="sub">No tracked bill activity this month.</p>
          )}
        </div>}

        {/* ---- by category ---- */}
        {!noData && <div className="card">
          <h2>{d.status === "in_progress" ? "Month to date by category" : "Spending by category"}</h2>
          {d.by_category.length === 0 && <p className="sub">{EMPTY_LINES.noSpending}</p>}
          {d.by_category.length > 0 && (
            <table><tbody>
              {d.by_category.map(([cat, amt, key]) => (
                <tr key={cat}>
                  {/* the label opens the month's rows in that category. The
                      filter matches the STORED key, which the row carries
                      beside its display label — sending the label itself
                      ("FOOD AND DRINK") matches nothing. A row with no key
                      is the clustered Amazon parent: an itemised bucket
                      rather than a category, so it has no filter to open
                      and stays plain text. */}
                  <td>{cat === "Amazon"
                    ? <><b>Amazon</b> <span className="sub">({d.amazon_subs.length})</span></>
                    : key
                      ? <Link className="catlink"
                              to={`/transactions?cat=${encodeURIComponent(key)}&y=${d.y}&m=${d.m}&counted=1`}>
                          {catLabel(cat)}</Link>
                      : catLabel(cat)}</td>
                  <td className="num">{money(amt)}</td>
                </tr>
              ))}
            </tbody></table>
          )}
        </div>}
      </div>

      {/* ---- biggest transactions ---- */}
      {!noData && <div className="card">
        <h2>Biggest transactions</h2>
        {d.biggest.length === 0 && <p className="sub">{EMPTY_LINES.noSpendingPosted}</p>}
        {d.biggest.length > 0 && (
          <table><tbody>
            {d.biggest.map((r, i) => (
              <tr key={i}>
                <td className="mut" style={{ whiteSpace: "nowrap", width: "1%" }}>{mmdd(r.date)}</td>
                <td className="num" style={{ whiteSpace: "nowrap", width: "1%" }}>{signedMoney(-r.amount, money)}</td>
                <td>{r.payee}</td>
                <td className="sub hide-m">{catLabel(r.category)}</td>
                <td className="mut hide-m" style={{ whiteSpace: "nowrap", textAlign: "right" }}>{r.account}</td>
              </tr>
            ))}
          </tbody></table>
        )}
      </div>}
    </div>
  );
}
