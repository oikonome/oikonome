// Today — ONE hero card (verdict pill + the big remaining number + the
// pace bar + the per-category left-today/rate tiles + the plan as a single
// flow line), a "Where it's going" money map with no competing headline,
// and ONE cash timeline card — recent posted activity flowing into the
// scheduled events across a TODAY divider, rows shown through the forecast
// low point with the rest behind an expander. Month-to-date is a one-line
// top-categories footer. Cash notices (quiet until it matters) and the Why
// block live in the hero.
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";
import Onboarding from "./Onboarding";
import { checklistOpen } from "../checklist";
import { api, dueLabel, errText, mmdd, money, type TodayFull, type Txn, catLabel } from "../api/client";
import { patchQueries, saveSettings } from "../api/cache";
import AreaChart from "../components/AreaChart";
import MoneyMap, { Track } from "../components/MoneyMap";
import { sentPlanRows, sentPlanTotal } from "../components/PlanBars";
import { FailedLine } from "../components/Loaded";
import { canEdit } from "../role";
import { localDayOf } from "../moneymath";
import PageSkeleton from "../components/PageSkeleton";
import SegmentedControl from "../components/SegmentedControl";
import Note from "../components/Note";
import ForecastRecord from "../components/ForecastRecord";

/** Card autopays as chart marks: the series is one point per day starting
 *  today, so a due date is found by its own index. A date the window does
 *  not cover is dropped rather than clamped to an edge — a line on the last
 *  day would claim a payment lands there. Several cards due the same day
 *  collapse into one line naming them all, because two labels at the same x
 *  are a smear. Shared with the Month lens and mirrored in the app's
 *  sparkline and the email chart. */
export function autopayMarks(series: [string, number][],
                             autopay: [string, number, string][] | undefined) {
  const at = new Map<number, string[]>();
  for (const [date, , name] of autopay ?? []) {
    const i = series.findIndex(([d]) => d === date);
    if (i < 0) continue;
    at.set(i, [...(at.get(i) ?? []), name]);
  }
  return [...at.entries()].map(([i, names]) => ({
    i, label: names.length > 2 ? `${names.length} autopays`
      : names.join(" + ") + " autopay",
  }));
}

/** the server's card due-date marks (todayview.due_marks) as chart events:
 *  one line per card per due date, its caption and the hovered day's line
 *  in the server's words */
export function dueMarks(series: [string, number][],
                         marks: { date: string; label: string; words: string }[]) {
  return marks.flatMap((m) => {
    const i = series.findIndex(([d]) => d === m.date);
    return i < 0 ? [] : [{ i, label: m.label, note: m.words }];
  });
}

// one row of the cash timeline after today, as the table prints it
type TimelineRow = {
  date: string; amount: number; label: string; bal_now: number | null;
  bal_stmt: number | null; kind: "event" | "month_end"; card?: string | null;
  // cash after card balances that day (the server's after_cards)
  after_cards?: number | null;
};

// shared with the Month/Year lenses
export const VERDICT = {
  "OVER BUDGET": { cls: "r", label: "OVER BUDGET" },
  "UNDER BUDGET": { cls: "g", label: "UNDER BUDGET" },
  // on plan is GOOD news — amber would read as a warning for the one
  // verdict that means everything is fine
  "ON BUDGET": { cls: "g", label: "ON PLAN" },
} as const;

// where a demo instance keeps the hero face, since it cannot keep it on
// the account (see the toggle below)
const FACE_KEY = "oiko-today-face";

// the most serious alert leads the needs-you line; its edge carries the
// severity the way the Alerts page's rows do
const SEV_RANK: Record<string, number> = { bad: 3, warn: 2, info: 1, good: 0 };
const sevRank = (sev: string) => SEV_RANK[sev] ?? 2;
const sevClass = (sev: string) =>
  sev === "bad" || sev === "info" || sev === "good" ? sev : "";

// date = a past day (YYYY-MM-DD) the user stepped back to; null = live
// today. The cache key must carry the date — every day is its own payload.
export default function Today({ date = null }: { date?: string | null }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["today", date ?? "now"],
                       queryFn: () => api.todayFull(date ?? undefined) });
  const ob = useQuery({ queryKey: ["onboarding"], queryFn: api.onboarding });
  // dismissing an alert is a tenant-wide write — editor chrome only
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const [allEvents, setAllEvents] = useState(false);
  // the card scenario the cash chart and timeline show; "Your usual" is
  // the server's headline and the default
  const [scenKey, setScenKey] = useState<string>("usual");
  // hero face override while the settings write lands — the server's
  // today_view is the durable value (mobile follows it; the daily
  // email's face is the separate email_schedule.daily.summary setting)
  const [faceOverride, setFaceOverride] = useState<"summary" | "detail" | null>(null);
  // fail CLOSED while /api/me loads — an editor briefly missing a control
  // beats a viewer flashing one that 403s (mobile's useViewer posture)
  const mayEdit = canEdit(me);
  // A demo instance refuses EVERY settings write (shared printed
  // credentials — one visitor must not re-face the page for everyone
  // after them), so the durable path would 403 here and the toggle would
  // flip and snap straight back. The choice is a view preference, not
  // household state, so on a demo it lives per-device instead: same
  // control, same instant response, remembered in localStorage across
  // navigations, never written to the server.
  const demo = !!me.data?.demo;
  // Read once, and never let Storage take the page down with it: access
  // THROWS under Safari's "block all cookies", some MDM policies and
  // partitioned iframes, this runs in render, and the SPA has no error
  // boundary — an unguarded read would blank the whole app for a
  // privacy-hardened visitor.
  // wizardexit.ts guards its own Storage calls for the same reason.
  const [perDevice] = useState<"summary" | "detail" | null>(() => {
    try {
      return localStorage.getItem(FACE_KEY) as "summary" | "detail" | null;
    } catch {
      return null;                      // storage blocked — session-only
    }
  });
  if (q.isPending) return <PageSkeleton />;
  if (q.isLoadingError) return <FailedLine q={q} what="Today" />;
  const d: TodayFull = q.data;
  // the stored face is the OWNER's own per-device choice, so it is read
  // back only for them: a viewer on a demo still sees the household's
  // saved face, which is the same contract the hidden toggle states.
  const adv = (faceOverride ?? (demo && mayEdit ? perDevice : null)
               ?? d.today_view) === "detail";
  const setFace = (f: "summary" | "detail") => {
    setFaceOverride(f);                       // instant; the write follows
    if (demo) {
      // per-device IS the persistence here; blocked storage just means the
      // choice lasts the session, which is still better than a 403
      try { localStorage.setItem(FACE_KEY, f); } catch { /* no storage */ }
      return;
    }
    // a view preference moves nothing in the verdict — seed the saved
    // settings and the cached faces directly rather than recomputing the
    // whole Today payload to learn a value we just wrote
    saveSettings(qc, { today_view: f })
      .then(() => patchQueries<TodayFull>(qc, ["today"],
                                          (t) => ({ ...t, today_view: f })))
      .catch((e) => {
        setFaceOverride(null);                // failed write ≠ new truth
        // silent snap-back looks like nothing happened — say so, the way
        // the alert-dismiss handler already does
        window.dispatchEvent(new CustomEvent("oiko-toast",
          { detail: `Couldn't save the view: ${errText(e)}` }));
      });
  };
  const v = VERDICT[d.verdict as keyof typeof VERDICT] ?? VERDICT["ON BUDGET"];
  // the hero's month figure: what is left of the month's spendable share,
  // and the words after it, composed once on the server (hero_face)
  const remaining = d.month_remaining_paced;
  const rw = d.runway;
  // no checking account (card-only or savings-only): cash ahead is
  // unknown, not $0 — the forecast's walk starts from nothing, so it is
  // not shown and no alarm is drawn from it. The runway says so outright;
  // an older payload by its null checking balance. (The server sends no
  // forecast at all for such a household.) (mobile: noCheckingLinked)
  const noChecking = !!rw?.no_checking || (!!rw && rw.checking == null);
  const fc = noChecking ? null : d.forecast;

  // setup is the owner's work — every step behind the checklist and
  // the wizard is a 403 for a viewer, so the card could only dead-end them
  const showChecklist = mayEdit && ob.data && checklistOpen(ob.data);

  // ---- cash: quiet until it matters — the notice the server decided
  // (todayview: state, the dollar it quotes, the date it runs to). The
  // forecast low still dates the timeline's cut below. ----
  const p0 = fc?.pace_stmt ?? null;
  // the three card scenarios as the server sent them (your usual, pay all
  // cards now, pay on the due date), each with its walk, low point in
  // words, chart lines and timeline rows — printed, never worked out here
  const scens = fc?.scenarios !== undefined ? (fc?.scenarios ?? []) : null;
  const scen = scens?.find((x) => x.key === scenKey) ?? scens?.[0] ?? null;
  const note = noChecking ? null : (d.cash_notice ?? null);
  // per-category tiles: X / Y — what's left TODAY of the daily rate that
  // finishes the month on budget — one framing, combined
  const tiles = ([["Food", d.allow.food], ["Everything else", d.allow.other],
    ...d.allow_kids.map((k) => [k.name, k] as const)] as const)
    // a parent whose whole budget went to its carve-outs would read
    // "$0 of $0"; it carries nothing, so the day's totals do not move
    .filter(([, a]) => a && !(a as { empty?: boolean }).empty);
  // the month on view, for the links a label opens
  // asOf: the day on view — a past day's tiles are that day's figures, so
  // the rows a label opens stop on that day too
  const period = { y: Number(d.date.slice(0, 4)), m: Number(d.date.slice(5, 7)),
                   asOf: d.date.slice(0, 10) };
  const ym = `&y=${period.y}&m=${period.m}&as_of=${period.asOf}`;
  // A tile's number is a verdict BUCKET, not a category: Food also counts
  // the food-override rows of merchants filed elsewhere (a warehouse club, an online store),
  // a custom tile is a plan label no transaction carries, and "Everything
  // else" is what the carve-outs left. So the label opens the ledger by
  // bucket — the same rows the number was summed from.
  const tileTo = (label: string) =>
    `/transactions?bucket=${encodeURIComponent(
      label === "Food" ? "food"
      : label === "Everything else" ? "other" : label)}${ym}`;

  // top-pane bars are DAY-scoped: today's spend against today's
  // allowance (todayview.simple_day_meter, the same bar as the email).
  // The month gauges live in the Monthly budget card. Rendered with the
  // map's Track so overspend keeps the divider + stripes idiom.
  const daySpent = d.day_meter?.spent ?? 0;
  const dayAllow = d.day_meter?.allow ?? 0;
  const dayRow = (spent: number, allow: number) => ({
    // an exhausted allowance with spend on it must read OVER (red past the
    // divider), not "unbudgeted" amber — floor the budget off zero
    label: "", judged: false, actual: spent, expected: 0,
    budget: spent > 0.005 ? Math.max(allow, 0.01) : allow,
  });

  // start of the recent window: yesterday, or today itself on the 1st
  const yesterday = d.yesterday ?? d.date;
  // the recent window's variable spending: the server's sum, the figure
  // the email prints
  const recentSum = d.recent_variable_spending ?? null;
  // the footnote's two kinds of missed bill: never posted, and posted then
  // withdrawn by the bank feed (the fourth slot; an older server omits it)
  const lateBills = d.overdue_unpaid.filter((o) => !o[3]);
  const goneBills = d.overdue_unpaid.filter((o) => !!o[3]);

  // ---- cash timeline: recent activity + scheduled events, one table ----
  // Past = the month's paid ✓ bill rows (they arrive in forecast_rows with
  // no balance) and the recent variable transactions; future = the
  // scheduled events, shown through the forecast low point by default.
  const paidRows = d.forecast_rows.filter(
    (r) => r.bal_now === null && r.bal_stmt === null && r.date <= d.date);
  // the selected scenario's rows after today; an older server's
  // forecast_rows otherwise (older-server path: stands in for `scenarios`)
  const futureRows: TimelineRow[] = scen
    ? scen.rows.map((r) => ({ date: r.date, amount: r.amount ?? 0, label: r.label,
                              bal_now: r.balance, bal_stmt: r.balance, kind: r.kind,
                              card: r.card ?? null, after_cards: r.after_cards }))
    : d.forecast_rows.filter((r) => !paidRows.includes(r));
  const lowDate = (scen ?? p0)?.min_date ?? null;
  // the month-end row always shows: the tile above quotes its balance
  const preLow = lowDate
    ? futureRows.filter((r) => r.kind === "month_end" || r.date <= lowDate)
    : futureRows;
  const shownFuture = allEvents || preLow.length === 0 ? futureRows : preLow;
  const hiddenCount = futureRows.length - shownFuture.length;
  // The card scenarios differ only in payment TIMING — converged lows mean
  // one balance column tells the whole story (mirrors the KPI collapse).
  const lows = fc ? [
    { label: "pay all cards now", short: "cards now", s: fc.pace_now },
    { label: "autopay statement", short: "autopay", s: fc.pace_stmt },
  ] : [];
  const converged = scen !== null || (lows.length > 0 &&
    lows.every((l) => l.s.min === lows[0].s.min
                      && l.s.min_date === lows[0].s.min_date));
  // balance columns: none without a forecast (every cell would be a dot —
  // the set-your-balance prompt in the hero already says why), one when
  // the card scenarios agree, two when they differ
  // cash after card balances beside the selected scenario's balance,
  // headed in the server's words; absent from an older server's rows
  const afterLabel = scen && fc?.after_cards_label
    && scen.rows.some((r) => r.after_cards != null) ? fc.after_cards_label : null;
  const balCols = !fc ? 0 : converged ? (afterLabel ? 2 : 1) : 2;
  const pastBal = balCols === 1
    ? <td className="num mut c-bal c-none">·</td>
    : balCols === 2
      ? <><td className="num mut hide-m">·</td>
          <td className="num mut c-bal c-none">·</td></>
      : null;

  const txnLabel = (r: Txn) =>
    r.item_summary ? <>{r.payee} — <i>{r.item_summary}</i></> : r.payee;

  return (
    <>
      {/* a two-factor recovery pending on this account — the notice
          can still be undone, and only Settings > Security cancels it */}
      {me.data?.factor_reset_pending && (
        <Note tone="bad" style={{ margin: "1rem 0 0" }}>
          <span style={{ flex: 1 }}>
            Someone asked for a recovery that removes this account's
            two-factor methods. It lands {mmdd(localDayOf(me.data.factor_reset_pending))}.
            {" "}Not you?{" "}
            <Link to="/settings/security">Cancel it in Security</Link>
          </span>
        </Note>
      )}
      {/* ---- past-day view (back-nav): the banner is THE way home. The
           forward-looking panes — cash forecast card/chart, alert strips,
           the cash notice — are now-facts, not that-day facts; the server
           already sends them empty (forecast/runway null, alerts []) so
           the conditionals below hide them without forking. ---- */}
      {!d.is_today && (
        <div className="note info" style={{ margin: "1rem 0 0" }}>
          <span style={{ flex: 1 }}>
            Viewing a past day — {mmdd(d.date)}. Cash forecast and alerts
            are hidden (they describe now, not this day).
          </span>
          <Link to="/" style={{ whiteSpace: "nowrap", fontWeight: 600 }}>
            ‹ back to today</Link>
        </div>
      )}
      {/* while the guided wizard is unfinished it is THE setup
          path — a parallel checklist whose links bypass it just competes.
          The checklist takes over once the wizard is done or dismissed.
          (Setup state is a now-fact too — not shown on a past day.) */}
      {/* an unread setup state is not "setup finished": the failed line
          sits where the setup card would */}
      {d.is_today && mayEdit && ob.data === undefined && ob.isError && (
        <FailedLine q={ob} what="the setup progress" />)}
      {d.is_today && showChecklist && (ob.data!.wizard_done
        ? <Onboarding data={ob.data!} hosted={!!me.data?.hosted} />
        : (
          <div className="card" style={{ marginTop: "1rem" }}>
            <h2 style={{ marginTop: 0 }}>Welcome — let's finish setting up</h2>
            <p className="mut" style={{ margin: ".2rem 0 .8rem" }}>
              The guided setup picks up right where you left off — connect
              accounts, bring in history, confirm bills, set budgets.</p>
            <Link to="/welcome"><button className="pri">
              Resume setup →</button></Link>
          </div>
        ))}
      {/* what needs you, as ONE line: how many alerts are waiting and
          the most serious one, opening the Alerts page — where each is
          fixed or dismissed with the same verbs. A bar per alert, each
          with its own buttons, pushed the day's number off a phone's
          first screen and said every alert twice in two vocabularies. */}
      {d.alerts.length > 0 && (() => {
        const worst = [...d.alerts].sort((x, y) =>
          sevRank(y.severity) - sevRank(x.severity))[0];
        const n = d.alerts.length;
        return (
          <Link to="/alerts" className={`needsrow ${sevClass(worst.severity)}`}>
            <b style={{ flexShrink: 0 }}>
              {n === 1 ? "1 thing needs you" : `${n} things need you`}</b>
            <span className="needsrow-msg">{worst.message}</span>
            <span className="needsrow-go" aria-hidden="true">›</span>
          </Link>
        );
      })()}

      {/* the face toggle sits OUTSIDE the cards and scopes the WHOLE
          page: summary = the verdict card IS the page,
          like the summary email; detail brings back the plan line, savings
          goals, the money map and the cash timeline. Remembered
          server-side, so mobile follows the same choice — except on a
          demo, which refuses settings writes, where it is per-device.
          Owner-only:
          today_view saves through /api/settings, which the server refuses
          for viewers, so an unguarded toggle would flip and silently snap
          back — viewers see the household's saved face, like the page's
          other write controls that hide rather than dead-end. */}
      {mayEdit && (
      <div style={{ display: "flex", justifyContent: "flex-end",
                    alignItems: "center", gap: ".5rem", marginTop: "1rem" }}>
        {/* a bare pair of words reads as a caption; the label and the
            capitals make it a control */}
        <span className="sub" id="today-view-label">View</span>
        <SegmentedControl labelledBy="today-view-label" size="sm"
          value={adv ? "detail" : "summary"} onChange={setFace}
          options={[{ value: "summary", label: "Summary" },
                    { value: "detail", label: "Detail" }]} />
      </div>
      )}

      {/* ---- 1. THE hero card: one big number (the verdict's own), the
           pace bar under it, the left-today tiles, the plan flow line —
           one pane, not a two-pane top row.

           Hidden entirely until budgets exist: with no
           plan the engine still produces a perfectly consistent "On
           budget · $0 of $0 left · $0 to spend today", which is
           arithmetic, not news — and it reads as a verdict on a plan the
           household has not made. The setup card above is the only thing
           worth showing at that point. Undefined onboarding data (still
           loading) keeps the hero, so it never flashes away. ---- */}
      {!(ob.data && !ob.data.budgets_set) && (
      <div className="card">
        {/* the verdict as ONE colored sentence: state word + the
            engine's pace phrase; the month total demotes
            to context on the right — it's the number you check second */}
        <div className="hero-verdict">
          <span className={`hv-state ${v.cls === "r" ? "neg" : "pos"}`}>
            {d.verdict === "OVER BUDGET" ? "Over budget"
              : d.verdict === "UNDER BUDGET" ? "Under budget" : "On budget"}
          </span>
          {/* pace carries the same good/bad color as the verdict word —
              it is the same signal, said in dollars */}
          {d.pace_line && (
            <span className={`hv-delta ${v.cls === "r" ? "neg" : "pos"}`}>
              {d.pace_line}</span>
          )}
          {/* the month's one figure on Today; its bars and the plan
              live on Budget, so the figure opens it */}
          <Link to="/budget" className="hv-month sub"
                style={{ color: "var(--mut)", textDecoration: "none" }}
                title="the month's budget, bar by bar">
            <b className={remaining < 0 ? "neg" : ""}>
              {money(Math.abs(remaining))}</b>
            {" "}{d.month_words} ›
          </Link>
        </div>

        {/* LEFT TO SPEND TODAY — the star. It's the only number that
            changes what you do today, so it gets the big type; the meter
            is today's allowance, not the month's. */}
        <div className="sub hero-todaylbl">{d.left_label}</div>
        {!adv && (<>
          {/* SIMPLE face (today_view, the default): the one number —
              still-spendable today across every variable bucket — a
              month meter, and a chip per bucket. Number and chip strings
              come composed from the server (d.simple), the same ones the
              daily email renders, so the surfaces cannot drift. */}
          <div className="hero-remaining">{money(d.simple.left_today)}</div>
          {/* full-width map-height track, DAY-scoped — no pace tick:
              the day is the unit */}
          <span style={{ display: "block", marginTop: ".4rem" }}
                title={`spent ${money(daySpent)} of ${money(dayAllow)} today`}>
            <Track row={dayRow(daySpent, dayAllow)} />
          </span>
          <div className="hw-chips" style={{ marginTop: ".7rem" }}>
            {d.simple.chips.map((c) => (
              <span key={c.text} className={`hw-chip ${c.tone}`}>
                {c.text}</span>
            ))}
          </div>
          {d.sweep_line && (
            <div className="sub" style={{ marginTop: ".4rem", fontSize: 12 }}>
              {d.sweep_line}</div>
          )}
        </>)}
        {adv && (
        <div>
          {tiles.map(([label, a]) => {
            const overT = a!.left_today < -0.5;
            // nothing to hand out and nothing spent (server-decided): ONE
            // line, not a big $0 over an empty bar and a caption saying it
            // again. Same words in the email and the app.
            if (a!.zero_day && !overT) return (
              <div key={label} style={{ marginTop: ".7rem", fontSize: 13 }}>
                <Link className="catlink" to={tileTo(label)}><b>{label}</b></Link>
                <span className="sub"> $0 today</span>
                {a!.daily_note && (<>
                  <span className="sub"> · </span>
                  <span className={`sub ${a!.rate < a!.plan ? "neg" : "pos"}`}>
                    {a!.daily_note}</span>
                </>)}
                {a!.recover_note && (
                  <span className="sub"> · {a!.recover_note}</span>)}
              </div>
            );
            // meter is DAY-scoped, not month-scoped: fill = today's spend
            // of today's allowance. The month gauges already live in the
            // Monthly budget card, so a month meter up here would say the
            // same thing twice. Numbers stack under the label — the whole pane keeps
            // one left-aligned anatomy, matching the simple face.
            return (
              <div key={label} className="drow" style={{ marginTop: ".7rem" }}>
                {/* the label opens the rows behind the number: the
                    category's ledger for the month on view */}
                <div className="drow-l"><Link className="catlink" to={tileTo(label)}><b>{label}</b></Link></div>
                {/* the number keeps the tiles' 26px weight — the row
                    layout is free to change, the number's prominence is
                    not */}
                {/* an over-budget row says so in the figure's place at
                    every width, as the app does; the lines under it are
                    the same at every width */}
                <div className={`htile-big drow-n ${overT ? "neg" : ""}`}>
                  {overT ? <>over by {money(-a!.left_today)}</>
                    : money(a!.left_dollars)}</div>
                {/* the line under the figure — the server's words, shown at
                    every width, as the app and the email show them */}
                <div className={`sub drow-s ${overT ? "neg" : ""}`}
                     style={{ fontSize: 12 }}>{a!.day_line}</div>
                <span style={{ display: "block", marginTop: 3 }}
                      title={`spent ${money(a!.spent_today)} of ${
                        money(a!.today_allowance)} today`}>
                  <Track row={dayRow(a!.spent_today, a!.today_allowance)} />
                </span>
                {/* what this category's daily budget was and what the
                    month's spending has moved it to — server-composed,
                    same words in the email. Colored by
                    the note's own polarity: a reduced daily is bad news,
                    a raised one good. rate < plan is exactly the server's
                    "reduced" (rounding is monotonic), so no string peeking */}
                {a!.daily_note && (
                  <div className={`sub ${
                    a!.rate < a!.plan ? "neg" : "pos"}`}
                       style={{ fontSize: 12, marginTop: 1 }}>{a!.daily_note}</div>
                )}
                {/* the question a reduced daily raises — how many no-spend
                    days bring it back — server-composed, same words in
                    the email */}
                {a!.recover_note && (
                  <div className="sub" style={{ fontSize: 12, marginTop: 1 }}>
                    {a!.recover_note}</div>
                )}
              </div>
            );
          })}
        </div>
        )}
        {/* PINNED BILLS — bills flagged "pin to Today" in their settings.
            Envelope pins say what's left in the pool this period; fixed
            pins say what's still to go out and when. Sentences are
            server-composed (bill_cards) — same words in the email. */}
        {(d.bill_cards ?? []).length > 0 && (
          <>
            <div className="sub hero-todaylbl" style={{ marginTop: ".8rem" }}>
              Pinned</div>
            <div>
              {d.bill_cards.map((c) => (
                <div key={c.label} style={{ marginTop: ".7rem" }}>
                  {/* number under the label, left — the pane's one
                      anatomy; the label opens the bill's history */}
                  <div><Link className="catlink"
                             to={"/bills/history?payee=" + encodeURIComponent(c.label)}>
                    <b>{c.label}</b></Link></div>
                  <div className={`htile-big ${c.over ? "neg" : ""}`}>
                    {money(c.left)}</div>
                  <div className="sub" style={{ fontSize: 12 }}>
                    {c.kind === "envelope" ? "left · " : "to go · "}{c.sub}
                  </div>
                  {/* post tick at today's position in the card's period
                      (month, or year for annual pools) — the money map's
                      pace idiom; full-width row like the category rows
                      above */}
                  <span style={{ position: "relative", display: "block",
                                 marginTop: 3 }}>
                    <span className="bartrack" style={{ display: "block" }}>
                      <i style={{ position: "absolute",
                                  inset: "0 auto 0 0",
                                  background: c.over
                                    ? "var(--red)" : "var(--green)",
                                  width: `${Math.max(3, c.frac * 100)}%` }} />
                    </span>
                    <i className="bartick"
                       style={{ left: `${Math.min(99, c.pace_frac * 100)}%` }} />
                  </span>
                  {c.status && (
                    <div className={`sub ${c.status_tone ?? ""}`}
                         style={{ fontSize: 12, marginTop: 1 }}>
                      {c.status}</div>
                  )}
                </div>
              ))}
            </div>
          </>
        )}
        {d.variable_scale && (
          <div className="sub" style={{ marginTop: ".4rem", color: "var(--amber)" }}>
            Variable budgets auto-scaled from {money(d.variable_scale.static_total)} to
            fit this month's bills.</div>
        )}
        {rw?.balance_unreliable ? (
          <div style={{ marginTop: ".6rem", borderTop: "1px solid var(--line)",
                        paddingTop: ".5rem", fontSize: ".85rem" }}>
            <b>Cash forecast is waiting on your balance</b> — imported
            accounts start at $0, so the numbers below can't be trusted
            yet. <Link to="/accounts">Set today's checking balance on
            Accounts</Link> and everything here comes alive.
          </div>
        ) : noChecking ? (
          <div style={{ marginTop: ".6rem", borderTop: "1px solid var(--line)",
                        paddingTop: ".5rem", fontSize: ".85rem" }}>
            <b>No checking account linked</b> — cash ahead starts from your
            checking balance, so there is nothing to forecast yet.{" "}
            <Link to="/accounts">Link your checking account on
            Accounts</Link> to see cash ahead.
          </div>
        ) : note?.state && note.ask ? (
          <div className="hero-watch"><span className="wdot" />
            <span><Link to="/bills">{note.ask}</Link></span>
          </div>
        ) : note?.state && note.text ? (
          // the sentence is the server's (todayview.cash_sentence): the
          // app and both emails print the same words
          <div className={`hero-watch ${note.state === "critical" ? "bad" : ""}`}>
            <span className="wdot" /><span>{note.text}</span>
          </div>
        ) : !noChecking && d.cash_band ? (
          // the quiet heavier-month line (todayview.cash_band): the usual
          // walk stays above $0, a heavier month would not
          <div className="hero-watch"><span className="wdot" />
            <span>{d.cash_band.text}</span></div>
        ) : null}
        {/* the WHY, over-budget only — a good day is QUIET, so this does
            not render on every verdict: one scannable row of merchant
            chips, the full
            per-category sentences behind an expander. Chips and sentences
            are server-composed so the three surfaces cannot drift. */}
        {(d.why_chips ?? []).length > 0 && (
          <div className="hero-why">
            <div className="hw-line">
              <span className="hw-k">Driving it:</span>
              <span className="hw-chips">
                {d.why_chips!.map((c) => (
                  <span key={c.payee} className="hw-chip" title={c.payee}>
                    <span className="hw-payee">{c.payee}</span>{" "}
                    <b className="neg">{money(c.amount)}</b>
                    {c.count > 1 && <span className="sub"> {c.count}×</span>}
                  </span>
                ))}
              </span>
            </div>
            {d.why && d.why.entries.length > 0 && (
              <details className="hw-more">
                <summary>by category ▸</summary>
                {d.why.entries.map((e) => (
                  <div key={e.name} style={{ margin: ".15rem 0" }}>
                    {/* the line's bucket is a tile's bucket: same door */}
                    <Link className="catlink" to={tileTo(e.name)}><b>{e.name}</b></Link>{" "}
                    <span className={e.tone === "over" ? "neg"
                      : e.tone === "under" ? "pos" : "sub"}>{e.text}</span>
                  </div>
                ))}
                {/* the server writes a recovery sentence when over;
                    mobile renders it too */}
                {d.why.recovery && (
                  <div className="sub" style={{ marginTop: ".3rem" }}>
                    {d.why.recovery}
                  </div>
                )}
              </details>
            )}
          </div>
        )}
        {/* "This month's plan" as ONE flow line — monthly facts,
            not daily news; Budget owns the detail */}
        {adv && d.plan_line && (() => {
          // the terms in the whole dollars the server rounded to the
          // printed excess (plan_line), so the line adds up as printed;
          // no plan line (no income yet) prints nothing in its place
          const pl = d.plan_line;
          return (
          <div className="planflow"
               style={{ marginTop: ".9rem", paddingTop: ".7rem",
                        borderTop: "1px solid var(--line)", fontSize: 13,
                        display: "flex", gap: ".55rem", flexWrap: "wrap",
                        alignItems: "baseline" }}>
            <span className="sub">Plan:</span>
            <span className="sub"><b style={{ color: "var(--ink)" }}>{money(pl.income)}</b> in</span>
            <span className="mut" aria-hidden="true">→</span>
            {/* the plan's own figures — bills evened over the year, the
                budgets as set — the basis the excess is computed from,
                so the chain adds up (same figures and words as the email) */}
            <span className="sub"><b style={{ color: "var(--ink)" }}>{money(pl.bills)}</b> {d.plan_bills_label ?? "bills"}
              {/* this month against the average — the server's words */}
              {d.bills_weight && <> ({d.bills_weight})</>}</span>
            <span className="mut" aria-hidden="true">→</span>
            <span className="sub"><b style={{ color: "var(--ink)" }}>{money(pl.to_spend)}</b> to spend</span>
            <span className="mut" aria-hidden="true">→</span>
            {pl.saved !== null && (<>
              <span className="sub"><b style={{ color: "var(--ink)" }}>{money(pl.saved)}</b> saved</span>
              <span className="mut" aria-hidden="true">→</span>
            </>)}
            {/* the excess in the Budget summary's words and tone — "planned
                excess", "· none this month" once the overrun has used it
                up (todayview.plan_excess_words) */}
            <span className="sub"><b className={d.plan_excess_words
                ? d.plan_excess_words.tone : pl.excess >= 0 ? "pos" : "neg"}>
              {money(pl.excess)}</b> {d.plan_excess_words?.text ?? "excess"}</span>
            {/* sweep goals sit beside the chain, not inside it — the
                sentence is server-composed so no surface can drift */}
            {d.sweep_line && (
              <span className="sub">· {d.sweep_line}</span>
            )}
            <Link to="/budget" style={{ marginLeft: "auto", fontSize: 12 }}
              title="rework income, budgets, carve-outs and the savings plan">
              balance the budget →</Link>
          </div>
          );
        })()}
      </div>
      )}

      {/* ---- savings goals — ledger-verified progress + pace.
           The card renders only when a TRACKABLE goal exists, or one
           that needs an account to become trackable — plan-only rows and
           the $0/$0 marker have nothing to report, so a card of only
           those is chrome, not information ---- */}
      {adv && (d.savings_goals ?? []).some(
          (g) => !g.plan_only || g.needs_account) && (
        <div className="card">
          <div className="sub" style={{ textTransform: "uppercase",
                letterSpacing: ".03em" }}>Savings goals</div>
          {d.savings_goals.filter((g) =>
              !(g.plan_only && !g.monthly_plan && !g.target)).map((g) => (
            <div key={g.name} style={{ marginTop: ".55rem" }}>
              <div style={{ display: "flex", gap: ".6rem", flexWrap: "wrap",
                            alignItems: "baseline" }}>
                <b>{g.name}</b>
                {g.plan_only ? (
                  <>
                    <span className="mut" style={{ fontSize: 13 }}>
                      plan {money(g.monthly_plan)}/mo</span>
                    <span className="mut" style={{ fontSize: 13,
                          marginLeft: "auto" }}>
                      {g.needs_account
                        ? "match words need an account — pick one on the "
                          + "Budget page"
                        : "plan only — set a destination account on the "
                          + "Budget page to track progress"}</span>
                  </>
                ) : (
                  <>
                    {/* open-ended goal (no target): "$X saved" + what
                        the last 3 months saved — never "of $0" */}
                    <span className="mut" style={{ fontSize: 13 }}>
                      {money(g.saved ?? 0)}{g.target ? <> of {money(g.target)}
                      </> : " saved"}
                      {g.pct != null && <> ({g.pct}%)</>}</span>
                    <span className="mut" style={{ fontSize: 13,
                          marginLeft: "auto" }}>
                      {!g.target
                        ? ((g.saved_90d ?? 0) > 0
                            ? `${money(g.saved_90d!)} in the last 3 months`
                            // withdrawals outweighing deposits is not
                            // the same news as nothing arriving
                            : g.contributed_90d
                              ? "no net saving in the last 3 months"
                              : "no contributions in 3 months")
                        : g.pct != null && g.pct >= 100 ? "funded ✓"
                        : g.on_pace === false
                          ? <b className="neg">off pace for {mmdd(g.target_date!)}</b>
                        : g.on_pace ? `on pace for ${mmdd(g.target_date!)}`
                        : g.eta ? `~${mmdd(g.eta)} at the current rate`
                        : g.contributed_90d
                          ? "no net saving in the last 3 months"
                        : "no contributions in 3 months"}</span>
                  </>
                )}
              </div>
              {!g.plan_only && !!g.target && (
                <span className="bartrack" style={{ display: "block",
                    marginTop: ".25rem" }}>
                  <i style={{ position: "absolute", inset: "0 auto 0 0",
                      background: "var(--green)",
                      width: `${Math.min(100, g.pct ?? 0)}%` }} />
                </span>
              )}
            </div>
          ))}
        </div>
      )}

      {/* ---- where it's going — the money map, bars only (its
           headline moved into the hero card; plan-only rows fold into the
           map's own footer line). DETAIL-ONLY: in summary the verdict
           card is the whole page, like the summary
           email. ---- */}
      {adv && (
      <div className="card">
        <h2>Monthly budget</h2>
        <MoneyMap
          rows={sentPlanRows(d.plan_rows ?? [])}
          total={sentPlanTotal(d.plan_total)}
          footnote={(d.overdue_unpaid.length > 0 || d.irregular_bills.length > 0) && (
            <div className="sub" style={{ fontSize: 12, marginTop: ".2rem" }}>
              {d.irregular_bills.length > 0 && (
                <>Non-monthly bills this month:{" "}
                  {d.irregular_bills.slice(0, 4).map((i, k) => (
                    <span key={k}>{i.payee} <b>{money(i.amount)}</b> ({i.label})
                      {k < Math.min(d.irregular_bills.length, 4) - 1 ? " · " : ""}</span>
                  ))}</>
              )}
              {d.irregular_bills.length > 0 && lateBills.length > 0 && <br />}
              {lateBills.length > 0 && (
                <>Scheduled but not yet posted:{" "}
                  {lateBills.slice(0, 4).map(([p, a, due], i) => (
                    <span key={i}>{p} {money(a)} (due {mmdd(due)})
                      {i < Math.min(lateBills.length, 4) - 1 ? ", " : ""}</span>
                  ))}</>
              )}
              {/* a charge the bank feed withdrew is not a bill that never
                  posted — its own line, in the email's words */}
              {(d.irregular_bills.length > 0 || lateBills.length > 0)
                && goneBills.length > 0 && <br />}
              {goneBills.length > 0 && (
                <span style={{ color: "var(--amber)" }}>withdrawn by the bank feed:{" "}
                  {goneBills.slice(0, 4).map(([p, a, , on], i) => (
                    <span key={i}>{p} {money(a)} ({mmdd(on)})
                      {i < Math.min(goneBills.length, 4) - 1 ? ", " : ""}</span>
                  ))}</span>
              )}
            </div>
          )} />
      </div>
      )}

      {/* ---- cash timeline: cash's ONE home —
           checking / cards / after-everything KPIs, the chart, and recent
           activity flowing into the scheduled events across a TODAY
           divider. Events show through the forecast low point; the rest
           behind the expander. Past-day / no-forecast views still get the
           recent-activity half. Detail-only, like the map. ---- */}
      {adv && (fc || d.recent.length > 0 || paidRows.length > 0) && (
        <div className="card">
          <h2>Cash timeline{" "}
            <span className="sub" style={{ fontWeight: 400 }}>
              {fc ? <>recent activity &amp; next {fc.days} days</>
                  : <>({mmdd(yesterday)} – {mmdd(d.date)} · variable spending {money(recentSum)})</>}
            </span></h2>
          {fc && (
            <div style={{ display: "flex", gap: "1.4rem", flexWrap: "wrap", alignItems: "baseline" }}>
              <div><span className="sub">checking now</span>
                <div className={`kpi sm ${fc.checking < 0 ? "neg" : ""}`}>{money(fc.checking)}</div></div>
              <div><span className="sub">card debt</span>
                <div className="kpi sm">{money(fc.card_debt)}</div></div>
              {/* paychecks still due, then the balance the month ends on —
                  the same day the timeline's month-end row shows
                  (todayview.month_end_left), with the server's note under
                  it: what it is after, or why no paycheck is counted */}
              {typeof d.income_due_total === "number" && (
                <div><span className="sub" title="paychecks still due this month">
                    paychecks still due</span>
                  <div className="kpi sm pos">+{money(d.income_due_total)}</div>
                  {(d.income_due_dates ?? []).length > 0 && (
                    <div className="sub" style={{ fontSize: 11 }}>
                      {(d.income_due_dates ?? []).slice(0, 3).map(mmdd).join(", ")}</div>)}
                </div>
              )}
              {typeof d.month_end_left === "number" && (
                <div><span className="sub">{d.month_end_label ?? "left at month end"}</span>
                  <div className={`kpi sm ${d.month_end_left >= 0 ? "pos" : "neg"}`}>
                    {money(d.month_end_left)}</div>
                  {d.month_end_note && (
                    <div className="sub" style={{ fontSize: 11, maxWidth: "16rem" }}>
                      {d.month_end_note}</div>)}
                  {/* what the cards still hold then, and the next card
                      payment after the month (todayview.month_end_left) */}
                  {d.month_end_cards_note && (
                    <div className="sub" style={{ fontSize: 11, maxWidth: "16rem" }}>
                      {d.month_end_cards_note}</div>)}
                </div>
              )}
              {/* each card scenario's low point, side by side in the
                  server's words (todayview.low_words) */}
              {scens ? scens.map((x) => (
                <div key={x.key}><span className="sub">low — {x.label.toLowerCase()}</span>
                  <div className={`kpi sm ${x.min < 0 ? "neg" : ""}`}
                       style={{ fontSize: 14 }}>{x.low_words}</div>
                  {/* what that walk still holds on the cards on its low
                      day (todayview.low_cards_note) */}
                  {x.low_cards_note && (
                    <div className="sub" style={{ fontSize: 11, maxWidth: "16rem" }}>
                      {x.low_cards_note}</div>)}
                </div>
              )) : converged ? (() => {
                const s = lows[0].s;
                const negs = lows.filter((l) => l.s.negative_date);
                return (
                  <div><span className="sub">low point</span>
                    <div className={`kpi sm ${s.min < 0 ? "neg" : ""}`}>
                      {money(s.min)} <span className="sub">{mmdd(s.min_date)}</span></div>
                    {negs.length > 0 && (
                      <div className="sub" style={{ fontSize: 11 }}>
                        {/* one shared date collapses too — "full balance
                            7/16 · cards now 7/16 · autopay 7/16" is noise */}
                        {negs.every((l) =>
                            l.s.negative_date === negs[0].s.negative_date)
                          ? `negative from ${mmdd(negs[0].s.negative_date!)}`
                          : "negative from: " + negs.map((l) =>
                              `${l.short} ${mmdd(l.s.negative_date!)}`).join(" · ")}
                      </div>
                    )}
                  </div>
                );
              })() : lows.map((l) => (
                <div key={l.label}><span className="sub">low — {l.label}</span>
                  <div className={`kpi sm ${l.s.min < 0 ? "neg" : ""}`}>
                    {money(l.s.min)} <span className="sub">{mmdd(l.s.min_date)}</span></div>
                  {l.s.negative_date && (
                    <div className="sub" style={{ fontSize: 11 }}>
                      negative from {mmdd(l.s.negative_date)}</div>
                  )}
                </div>
              ))}
            </div>
          )}
          {/* fc_chart as a static line: h=220, zero_line, fill,
              critical-date marker (todayview.build_context):
              autopay statement is the primary series */}
          {/* one selector for the chart and the timeline below it */}
          {fc && scens && scens.length > 1 && (
            <SegmentedControl label="Card payments" size="sm"
              value={scen?.key ?? null} onChange={setScenKey}
              style={{ margin: ".4rem 0 .2rem" }}
              options={scens.map((x) => ({ value: x.key, label: x.label }))} />
          )}
          {fc && scen && (
            <AreaChart variant="static" height={220}
              subject={`Cash timeline for the next ${fc.days} days, ${scen.label.toLowerCase()}`}
              points={scen.series}
              zeroLine
              markX={(() => { const i = scen.series.findIndex(([, v]) => v < 0);
                              return i === -1 ? null : i; })()}
              markLabel={scen.runs_out_label}
              // each card's due dates in every scenario; the dated payments
              // (older-server path: stands in for `due_marks`)
              events={scen.due_marks !== undefined
                ? dueMarks(scen.series, scen.due_marks ?? [])
                : autopayMarks(scen.series, scen.marks)}
              // the range the forecast's own past misses suggest,
              // shaded, once the server sends it; the same walk in a
              // heavier month, dashed beside it, until then
              band={scen.band ?? undefined}
              second={scen.band ? undefined : scen.heavy ?? undefined}
              labels={[scen.label, scen.band ? scen.band_label ?? ""
                                             : scen.heavy_label ?? ""]}
              // the hovered day lists the other scenarios' balances too
              tipExtra={(scens ?? []).filter((x) => x.key !== scen.key)
                .map((x) => ({ name: x.label, points: x.series }))} />
          )}
          {/* older-server path: stands in for `scenarios` */}
          {fc && !scens && (
            <AreaChart variant="static" height={220}
              subject={`Cash timeline for the next ${fc.days} days, autopay statement balance`}
              points={fc.pace_stmt.series}
              second={fc.pace_now.series}
              zeroLine
              markX={(() => { const i = fc.pace_stmt.series.findIndex(([, v]) => v < 0);
                              return i === -1 ? null : i; })()}
              // the server's mark, in the page's MM/DD/YY
              markLabel={fc.runs_out_label}
              events={autopayMarks(fc.pace_stmt.series, fc.card_autopay)}
              labels={["autopay statement balance", "pay all cards now"]} />
          )}
          {/* the one-offs the daily pace leaves out — the server's
              sentence, the app's and the email's */}
          {fc?.pace_note && (
            <div className="sub" style={{ fontSize: 12, marginTop: ".3rem" }}>
              {fc.pace_note}</div>
          )}
          {/* the band's sentence while it is drawn — the server's */}
          {scen?.band_text && (
            <div className="sub" style={{ fontSize: 12, marginTop: ".3rem" }}>
              {scen.band_text}</div>
          )}
          {/* how close the forecast has landed (todayview.score_sentences):
              the line opens the record; the lean and the first weeks'
              note print as sent */}
          {fc?.score?.text && <ForecastRecord text={fc.score.text} />}
          {fc?.score?.bias_text && (
            <div className="sub" style={{ fontSize: 12, marginTop: ".3rem" }}>
              {fc.score.bias_text}</div>
          )}
          {fc?.score?.note && (
            <div className="sub" style={{ fontSize: 12, marginTop: ".3rem" }}>
              {fc.score.note}</div>
          )}
          {/* card spending the selected walk still owes when it ends */}
          {scen?.cards_note && (
            <div className="sub" style={{ fontSize: 12, marginTop: ".3rem" }}>
              {scen.cards_note}</div>
          )}
          {/* when card spending reaches checking — the server's words */}
          {fc?.card_timing_note && (
            <div className="sub" style={{ fontSize: 12, marginTop: ".3rem" }}>
              {fc.card_timing_note}</div>
          )}
          {/* a paycheck the forecast leaves out because it stopped
              arriving — the server's words */}
          {fc?.stopped_paychecks_note && (
            <div className="sub" style={{ fontSize: 12, marginTop: ".3rem" }}>
              {fc.stopped_paychecks_note}</div>
          )}
          {/* own-account transfers the forecast leaves out — the
              server's words (todayview.transfers_note) */}
          {fc?.transfers_note && (
            <div className="sub" style={{ fontSize: 12, marginTop: ".3rem" }}>
              {fc.transfers_note}</div>
          )}
          {/* .cash-tl + per-td c-* classes: index.css restacks each row
              into a two-line card at phone widths (the TxnTable pattern) —
              the nowrap/ellipsis cells otherwise squeeze the Event column
              to nothing on a phone. Desktop rendering is untouched. */}
          <table className="cash-tl" style={{ marginTop: ".6rem" }}>
            <thead><tr><th>Date</th><th>Event</th><th className="num">Amount</th>
              {afterLabel ? <><th className="num hide-m">{afterLabel}</th>
                    <th className="num">Balance</th></>
                : balCols === 1 ? <th className="num">Balance</th>
                : balCols === 2 ? <><th className="num hide-m">Pay cards now</th>
                    <th className="num">Autopay stmt</th></> : null}
            </tr></thead>
            <tbody>
              {paidRows.map((r, i) => (
                <tr key={`p${i}`}>
                  <td className="mut c-date" style={{ whiteSpace: "nowrap", width: "1%" }}>{mmdd(r.date)}</td>
                  <td className="mut c-label" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.label}</td>
                  <td className="num mut c-amt" style={{ whiteSpace: "nowrap" }}>{money(r.amount)}</td>
                  {pastBal}
                </tr>
              ))}
              {d.recent.map((r) => (
                <tr key={r.id}>
                  <td className="mut c-date" style={{ whiteSpace: "nowrap", width: "1%" }}>{mmdd(r.date)}</td>
                  <td className="c-label" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    <Link to="/transactions">{txnLabel(r)}</Link>
                    {r.pending ? <span className="pill m" style={{ marginLeft: 6 }}>pend</span> : null}
                    {/* its own line on a phone (.tl-cat), where a dot
                        would otherwise end the line above it */}
                    <span className="sub tl-cat"><span className="tl-dot"> · </span>{catLabel(r.category)}</span>
                  </td>
                  <td className={`num c-amt ${r.amount < 0 ? "pos" : ""}`}
                      style={{ whiteSpace: "nowrap" }}>{money(-r.amount, true)}</td>
                  {pastBal}
                </tr>
              ))}
              {fc && (
                <tr className="tr-today">
                  <td colSpan={3 + balCols}
                      style={{ borderBottom: "1px solid var(--blue)",
                               padding: ".3rem .55rem" }}>
                    <b style={{ fontSize: 12, color: "var(--blue)" }}>TODAY</b>
                    <span className="sub"> · checking {money(fc.checking)} ·
                      recent variable spending {money(recentSum)}</span>
                  </td>
                </tr>
              )}
              {shownFuture.map((r, i) => (
                <tr key={`f${i}`}>
                  <td className="mut c-date" style={{ whiteSpace: "nowrap", width: "1%" }}>{dueLabel(r.date, d.date)}</td>
                  {/* a bill on a card is listed on its day and moves no
                      checking balance: greyed like a paid row */}
                  <td className={`c-label ${r.bal_now === null || r.card ? "mut" : ""}`}
                      style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.label}</td>
                  {/* the month-end row is a balance with no amount */}
                  <td className={`num c-amt ${r.bal_now === null || r.card || r.kind === "month_end" ? "mut" : r.amount > 0 ? "pos" : ""}`}
                      style={{ whiteSpace: "nowrap" }}>{r.kind === "month_end" ? "" : money(r.amount)}</td>
                  {/* without checking the walk's balances start from
                      nothing — the events stand, their balances do not */}
                  {converged ? (
                    <>
                      {afterLabel && (
                        <td className={`num hide-m ${!noChecking && r.after_cards != null && r.after_cards < 0 ? "neg" : "mut"}`}
                            style={{ whiteSpace: "nowrap" }}>{!noChecking && r.after_cards != null ? money(r.after_cards) : "·"}</td>
                      )}
                      <td className={`num c-bal ${r.bal_stmt !== null && r.bal_stmt < 0 ? "neg" : "mut"}`}
                          style={{ whiteSpace: "nowrap" }}>{r.bal_stmt !== null ? money(r.bal_stmt) : "·"}</td>
                    </>
                  ) : (
                    <>
                      <td className={`num hide-m ${!noChecking && r.bal_now !== null && r.bal_now < 0 ? "neg" : "mut"}`}
                          style={{ whiteSpace: "nowrap" }}>{!noChecking && r.bal_now !== null ? money(r.bal_now) : "·"}</td>
                      <td className={`num c-bal ${!noChecking && r.bal_stmt !== null && r.bal_stmt < 0 ? "neg" : "mut"}`}
                          style={{ whiteSpace: "nowrap" }}>{!noChecking && r.bal_stmt !== null ? money(r.bal_stmt) : "·"}</td>
                    </>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          {hiddenCount > 0 && !allEvents && (
            <button style={{ marginTop: ".55rem", borderRadius: 999,
                             padding: ".22rem .8rem", fontSize: 13 }}
                    onClick={() => setAllEvents(true)}>
              all {futureRows.length} events →</button>
          )}
        </div>
      )}


    </>
  );
}
