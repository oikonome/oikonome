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
import { api, errText, mmdd, money, type AlertRow, type TodayFull, type Txn, catLabel } from "../api/client";
import { patchList, patchQueries, removeFromList, saveSettings } from "../api/cache";
import AreaChart from "../components/AreaChart";
import MoneyMap, { Track } from "../components/MoneyMap";
import { planRows } from "../components/PlanBars";
import { canEdit } from "../role";

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
  if (q.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (q.isError) return <div className="note bad">Couldn't load: {String(q.error)}</div>;
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
  const remaining = d.variable_budget - d.variable_actual;
  const fc = d.forecast;

  // setup is the owner's work — every step behind the checklist and
  // the wizard is a 403 for a viewer, so the card could only dead-end them
  const showChecklist = mayEdit && ob.data && (ob.data.transactions === 0 ||
    ob.data.accounts === 0 || !ob.data.budgets_set || ob.data.bills === 0);

  // ---- cash: quiet until it matters — three states ----
  // autopay-statement is the realistic with-cards trajectory.
  // OK = silence. Tight = one calm line (headroom under ~a paycheck, or
  // the forecast dips under $0 beyond 14 days). Critical = red, with an
  // action (short today, or under $0 within 14 days).
  const p0 = fc?.pace_stmt ?? null;
  const daysTo = (iso: string) => Math.round(
    (new Date(iso).getTime() - new Date(d.date).getTime()) / 86400000);
  const negIn = p0?.negative_date ? daysTo(p0.negative_date) : null;
  const paycheckish = d.income ? d.income / 2 : 1000;
  const cashCritical = (d.headroom !== null && d.headroom < 0) ||
    (negIn !== null && negIn <= 14);
  const cashTight = !cashCritical &&
    ((d.headroom !== null && d.headroom < paycheckish) ||
     (negIn !== null || !!(p0 && p0.min < 0)));
  // the shortfall quoted beside negative_date is the balance ON that day
  // (first_neg_amount) — `min` can be a far deeper trough weeks later, and
  // pairing min with negative_date welded the wrong dollar to the wrong day
  const firstNeg = p0?.negative_date
    ? ((p0 as { min: number; first_neg_amount?: number | null })
        .first_neg_amount ?? p0.min)
    : null;
  // "slow spending to $X/day": the daily burn that clears the shown dip
  const slowTo = firstNeg !== null && firstNeg < 0 && negIn && negIn > 0
    ? Math.max(0, (p0!.rate ?? 0) + firstNeg / negIn) : null;

  const rw = d.runway;

  // per-category tiles: X / Y — what's left TODAY of the daily rate that
  // finishes the month on budget — one framing, combined
  const tiles = ([["Food", d.allow.food], ["Everything else", d.allow.other],
    ...d.allow_kids.map((k) => [k.name, k] as const)] as const)
    .filter(([, a]) => a);
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
  // allowance. The month gauges live in the Monthly budget card —
  // repeating them up here would say the same thing twice. Rendered with the
  // map's Track so overspend keeps the divider + stripes idiom.
  // Mirrors the server's simple_day_meter (todayview.py) so this bar,
  // the headline and the email always agree: spend counts against the
  // bar only up to each bucket's own allowance — an over bucket's excess
  // can't be absorbed by a neighbour's unused room — and once every
  // bucket is exhausted the true overshoot returns so the bar reads over
  // exactly when the day as a whole is.
  // left_today is the server's own rounded per-bucket remainder — summing
  // it (not re-deriving allowance − spent) keeps this bar equal to the
  // headline to the cent.
  const dayAllow = tiles.reduce((s, [, a]) => s + a!.today_allowance, 0);
  const dayLeft = tiles.reduce((s, [, a]) =>
    s + Math.max(0, a!.left_today), 0);
  const daySpent = dayAllow - dayLeft + (dayLeft < 0.005
    ? tiles.reduce((s, [, a]) => s + Math.max(0, -a!.left_today), 0)
    : 0);
  const dayRow = (spent: number, allow: number) => ({
    // an exhausted allowance with spend on it must read OVER (red past the
    // divider), not "unbudgeted" amber — floor the budget off zero
    label: "", judged: false, actual: spent, expected: 0,
    budget: spent > 0.005 ? Math.max(allow, 0.01) : allow,
  });

  // start of the recent window: yesterday, or today itself on the 1st
  const yesterday = d.yesterday ?? d.date;
  const recentSum = d.recent.reduce((s, r) => s + r.amount, 0);

  // ---- cash timeline: recent activity + scheduled events, one table ----
  // Past = the month's paid ✓ bill rows (they arrive in forecast_rows with
  // no balance) and the recent variable transactions; future = the
  // scheduled events, shown through the forecast low point by default.
  const paidRows = d.forecast_rows.filter(
    (r) => r.bal_now === null && r.bal_stmt === null && r.date <= d.date);
  const futureRows = d.forecast_rows.filter((r) => !paidRows.includes(r));
  const lowDate = p0?.min_date ?? null;
  const preLow = lowDate ? futureRows.filter((r) => r.date <= lowDate) : futureRows;
  const shownFuture = allEvents || preLow.length === 0 ? futureRows : preLow;
  const hiddenCount = futureRows.length - shownFuture.length;
  // The card scenarios differ only in payment TIMING — converged lows mean
  // one balance column tells the whole story (mirrors the KPI collapse).
  const lows = fc ? [
    { label: "pay all cards now", short: "cards now", s: fc.pace_now },
    { label: "autopay statement", short: "autopay", s: fc.pace_stmt },
  ] : [];
  const converged = lows.length > 0 &&
    lows.every((l) => l.s.min === lows[0].s.min
                      && l.s.min_date === lows[0].s.min_date);

  const txnLabel = (r: Txn) =>
    r.item_summary ? <>{r.payee} — <i>{r.item_summary}</i></> : r.payee;

  return (
    <>
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
      {d.alerts.map((a, i) => {
        // BOTH keys, like the Alerts page already does: the strip reads
        // ["today"], but the header bell's count reads ["alertsHistory"]
        // with a 60s staleTime, so clearing only the first removes the
        // alert and leaves the badge still showing 1.
        // Both are patched in place, up front: a dismissal has one
        // outcome, and waiting on a refetch of the full Today payload to
        // prove it left the note sitting there — and a second click on it
        // posting again. The strip goes back only if the server refuses.
        const dismiss = async () => {
          const todayKey = ["today", date ?? "now"];
          // a focus/poll refetch already in flight would land after the
          // patch and put the alert back with nothing left to reconcile it
          await qc.cancelQueries({ queryKey: todayKey });
          const prev = qc.getQueryData<TodayFull>(todayKey);
          const same = (x: { kind: string; message: string }) =>
            x.kind === a.kind && x.message === a.message;
          removeFromList<TodayFull, TodayFull["alerts"][number]>(
            qc, todayKey, "alerts", same);
          patchList<{ alerts: AlertRow[] }, AlertRow>(
            qc, ["alertsHistory"], "alerts", same, { dismissed: 1 });
          return api.alertDismiss(a.kind, a.message)
            .catch((e) => {
              if (prev) qc.setQueryData(todayKey, prev);
              qc.invalidateQueries({ queryKey: ["alertsHistory"] });
              window.dispatchEvent(new CustomEvent("oiko-toast",
                { detail: `Dismiss failed: ${errText(e)}` }));
            });
        };
        return (
        <div key={i} className={`note ${a.severity === "bad" ? "bad" : a.severity === "info" ? "info" : a.severity === "good" ? "good" : ""}`}
             style={{ margin: "1rem 0 0" }}>
          <span style={{ flex: 1 }}>
            {a.severity === "good" ? "✓" : a.severity === "info" ? "•" : "⚠"}{" "}
            {/* An alert that names something you'd want to change links to
                the page that changes it (drift → that bill's history), and
                FOLLOWING that link counts as acting on it: the strip is for
                things still needing attention, so an alert you have gone and
                dealt with must not still be sitting there when you come
                back. It lands in the alert history like any dismissal,
                restorable from there. Viewers can't dismiss, so
                for them it stays a plain link. */}
            {a.link ? (
              <Link to={a.link} onClick={() => { if (mayEdit) dismiss(); }}>
                {a.message}</Link>
            ) : a.message}
          </span>
          {mayEdit && (
          <button className="dismiss" title="dismiss"
                  onClick={dismiss}>✕</button>
          )}
        </div>
        );
      })}

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
                    marginTop: "1rem" }}>
        <span style={{ display: "inline-flex", borderRadius: 999,
                       border: "1px solid var(--line)", overflow: "hidden",
                       fontSize: 12.5 }}>
          {(["summary", "detail"] as const).map((f) => {
            const on = (adv ? "detail" : "summary") === f;
            return (
              <button key={f} onClick={() => setFace(f)}
                style={{ background: on ? "var(--hover)" : "none",
                         border: "none", cursor: "pointer", font: "inherit",
                         fontSize: 12.5, padding: ".28rem .85rem",
                         color: on ? "var(--ink)" : "var(--mut)",
                         fontWeight: on ? 600 : 400 }}>{f}</button>
            );
          })}
        </span>
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
          <span className="hv-month sub">
            <b className={remaining < 0 ? "neg" : ""}>
              {money(Math.abs(remaining))}</b>
            {remaining < 0 ? " over the " : " of "}{money(d.variable_budget)}
            {remaining < 0 ? "" : " left"} · {d.days_left}{" "}
            day{d.days_left === 1 ? "" : "s"}
          </span>
        </div>

        {/* LEFT TO SPEND TODAY — the star. It's the only number that
            changes what you do today, so it gets the big type; the meter
            is today's allowance, not the month's. */}
        <div className="sub hero-todaylbl">Left to spend today</div>
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
            // meter is DAY-scoped, not month-scoped: fill = today's spend
            // of today's allowance. The month gauges already live in the
            // Monthly budget card, so a month meter up here would say the
            // same thing twice. Numbers stack under the label — the whole pane keeps
            // one left-aligned anatomy, matching the simple face.
            return (
              <div key={label} style={{ marginTop: ".7rem" }}>
                {/* the label opens the rows behind the number: the
                    category's ledger for the month on view */}
                <div><Link className="catlink" to={tileTo(label)}><b>{label}</b></Link></div>
                {/* the number keeps the tiles' 26px weight — the row
                    layout is free to change, the number's prominence is
                    not */}
                <div className={`htile-big ${overT ? "neg" : ""}`}>
                  {money(Math.max(0, a!.left_today))}</div>
                <div className="sub" style={{ fontSize: 12 }}>
                  {overT
                    ? <>over today by <b className="neg">
                        {money(-a!.left_today)}</b> · spent{" "}
                        {money(a!.spent_today)} of {money(a!.today_allowance)}</>
                    : a!.spent_today > 0.005
                      ? <>of {money(a!.today_allowance)} today · spent{" "}
                          {money(a!.spent_today)}</>
                      : <>of {money(a!.today_allowance)} today · nothing
                          spent yet</>}
                </div>
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
              Pinned bills</div>
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
        ) : cashCritical ? (
          <div className="hero-watch bad"><span className="wdot" />
            <span>
            {d.headroom !== null && d.headroom < 0 ? (
              <>Short <b>{money(-d.headroom)}</b> today — checking doesn't
                cover cards &amp; bills before the next paycheck. Cover it from
                savings{slowTo !== null
                  ? <>, or slow spending to <b>{money(slowTo)}/day</b></>
                  : ""}.</>
            ) : (
              <>Short <b>{money(-(firstNeg ?? p0!.min))}</b> by{" "}
                <b>{p0!.negative_date ? mmdd(p0!.negative_date) : "now"}</b> —
                cover it from savings{slowTo !== null
                  ? <>, or slow spending to <b>{money(slowTo)}/day</b></>
                  : ""}.</>
            )}
            </span>
          </div>
        ) : cashTight ? (
          <div className="hero-watch"><span className="wdot" />
            <span>
            {p0 && (p0.min < 0 || negIn !== null)
              ? <><b>Cash gets tight</b> around {mmdd(p0.min_date)}{" "}
                  (low {money(p0.min)}).</>
              : <><b>Cash is tight</b> — {money(d.headroom)} left after
                  cards &amp; bills before the next paycheck.</>}
            </span>
          </div>
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
        {adv && d.plan_surplus !== null && (
          <div className="planflow"
               style={{ marginTop: ".9rem", paddingTop: ".7rem",
                        borderTop: "1px solid var(--line)", fontSize: 13,
                        display: "flex", gap: ".55rem", flexWrap: "wrap",
                        alignItems: "baseline" }}>
            <span className="sub">Plan:</span>
            <span className="sub"><b style={{ color: "var(--ink)" }}>{money(d.income)}</b> in</span>
            <span style={{ color: "var(--line)" }}>→</span>
            <span className="sub"><b style={{ color: "var(--ink)" }}>{money(d.buckets.fixed.month_budget)}</b> bills</span>
            <span style={{ color: "var(--line)" }}>→</span>
            <span className="sub"><b style={{ color: "var(--ink)" }}>{money(d.variable_budget)}</b> to spend</span>
            <span style={{ color: "var(--line)" }}>→</span>
            {(d.savings_plan ?? 0) > 0.005 && (<>
              <span className="sub"><b style={{ color: "var(--ink)" }}>{money(d.savings_plan!)}</b> saved</span>
              <span style={{ color: "var(--line)" }}>→</span>
            </>)}
            <span className="sub"><b className={d.plan_surplus >= 0 ? "pos" : "neg"}>
              {money(d.plan_surplus)}</b> excess</span>
            {/* sweep goals sit beside the chain, not inside it — the
                sentence is server-composed so no surface can drift */}
            {d.sweep_line && (
              <span className="sub">· {d.sweep_line}</span>
            )}
            <Link to="/budget" style={{ marginLeft: "auto", fontSize: 12 }}
              title="rework income, budgets, carve-outs and the savings plan">
              balance the budget →</Link>
          </div>
        )}
      </div>
      )}

      {/* ---- savings goals — ledger-verified progress + pace.
           The card renders only when a TRACKABLE goal exists — plan-only
           rows and the $0/$0 marker have nothing to report, so a card of
           only those is chrome, not information ---- */}
      {adv && (d.savings_goals ?? []).some((g) => !g.plan_only) && (
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
                      plan only — set a destination account on the Budget
                      page to track progress</span>
                  </>
                ) : (
                  <>
                    {/* open-ended goal (no target): "$X saved" + the
                        measured rate — never "of $0" */}
                    <span className="mut" style={{ fontSize: 13 }}>
                      {money(g.saved ?? 0)}{g.target ? <> of {money(g.target)}
                      </> : " saved"}
                      {g.pct != null && <> ({g.pct}%)</>}</span>
                    <span className="mut" style={{ fontSize: 13,
                          marginLeft: "auto" }}>
                      {!g.target
                        ? ((g.rate_90d ?? 0) > 0
                            ? `saving ${money(g.rate_90d!)}/mo`
                            : "no contributions in 90 days")
                        : g.pct != null && g.pct >= 100 ? "funded ✓"
                        : g.on_pace === false
                          ? <b className="neg">off pace for {mmdd(g.target_date!)}</b>
                        : g.on_pace ? `on pace for ${mmdd(g.target_date!)}`
                        : g.eta ? `~${mmdd(g.eta)} at the current rate`
                        : "no contributions in 90 days"}</span>
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
          rows={planRows(d.buckets, { period,
            fixedActual: d.buckets.fixed.actual,
            fixedBudget: d.buckets.fixed.month_budget,
            fixedPending: d.fixed_unpaid_due,
            savings: (() => {
              const g = (d.savings_goals ?? []).find((x) => x.name === "Savings");
              return g ? { plan: g.monthly_plan, saved: g.rate_90d } : null;
            })(),
            excess: d.plan_surplus,
          })}
          footnote={(d.overdue_unpaid.length > 0 || d.irregular_bills.length > 0) && (
            <div className="sub" style={{ fontSize: 12, marginTop: ".2rem" }}>
              {d.irregular_bills.length > 0 && (
                <>Non-monthly bills this month:{" "}
                  {d.irregular_bills.slice(0, 4).map((i, k) => (
                    <span key={k}>{i.payee} <b>{money(i.amount)}</b> ({i.label})
                      {k < Math.min(d.irregular_bills.length, 4) - 1 ? " · " : ""}</span>
                  ))}</>
              )}
              {d.irregular_bills.length > 0 && d.overdue_unpaid.length > 0 && <br />}
              {d.overdue_unpaid.length > 0 && (
                <>Scheduled but not yet posted:{" "}
                  {d.overdue_unpaid.slice(0, 4).map(([p, a, due], i) => (
                    <span key={i}>{p} {money(a)} (due {mmdd(due)})
                      {i < Math.min(d.overdue_unpaid.length, 4) - 1 ? ", " : ""}</span>
                  ))}</>
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
              {/* typeof guard: a mixed-deploy payload without
                  the field must not render $NaN */}
              {rw && rw.checking !== null && typeof d.bills_remaining_month === "number" && (
                <div><span className="sub"
                    title="checking − card debt − bills still due this month">
                    after cards &amp; bills</span>
                  <div className={`kpi sm ${rw.checking - rw.card_debt
                        - d.bills_remaining_month >= 0 ? "pos" : "neg"}`}>
                    {money(rw.checking - rw.card_debt
                           - d.bills_remaining_month)}</div></div>
              )}
              {converged ? (() => {
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
          {fc && (
            <AreaChart variant="static" height={220}
              points={fc.pace_stmt.series}
              second={fc.pace_now.series}
              zeroLine
              markX={(() => { const i = fc.pace_stmt.series.findIndex(([, v]) => v < 0);
                              return i === -1 ? null : i; })()}
              markLabel={fc.pace_stmt.negative_date
                ? "runs out " + mmdd(fc.pace_stmt.negative_date).replace(/^0/, "").replace(/\/0/, "/")
                : ""}
              events={autopayMarks(fc.pace_stmt.series, fc.card_autopay)}
              labels={["autopay statement balance", "pay all cards now"]} />
          )}
          {/* .cash-tl + per-td c-* classes: index.css restacks each row
              into a two-line card at phone widths (the TxnTable pattern) —
              the nowrap/ellipsis cells otherwise squeeze the Event column
              to nothing on a phone. Desktop rendering is untouched. */}
          <table className="cash-tl" style={{ marginTop: ".6rem" }}>
            <thead><tr><th>Date</th><th>Event</th><th className="num">Amount</th>
              {converged || !fc
                ? <th className="num">Balance</th>
                : <><th className="num hide-m">Pay cards now</th>
                    <th className="num">Autopay stmt</th></>}
            </tr></thead>
            <tbody>
              {paidRows.map((r, i) => (
                <tr key={`p${i}`}>
                  <td className="mut c-date" style={{ whiteSpace: "nowrap", width: "1%" }}>{mmdd(r.date)}</td>
                  <td className="mut c-label" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.label}</td>
                  <td className="num mut c-amt" style={{ whiteSpace: "nowrap" }}>{money(r.amount)}</td>
                  {converged || !fc
                    ? <td className="num mut c-bal">·</td>
                    : <><td className="num mut hide-m">·</td>
                        <td className="num mut c-bal">·</td></>}
                </tr>
              ))}
              {d.recent.map((r) => (
                <tr key={r.id}>
                  <td className="mut c-date" style={{ whiteSpace: "nowrap", width: "1%" }}>{mmdd(r.date)}</td>
                  <td className="c-label" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    <Link to="/transactions">{txnLabel(r)}</Link>
                    {r.pending ? <span className="pill m" style={{ marginLeft: 6 }}>pend</span> : null}
                    <span className="sub"> · {catLabel(r.category)}</span>
                  </td>
                  <td className={`num c-amt ${r.amount < 0 ? "pos" : ""}`}
                      style={{ whiteSpace: "nowrap" }}>{money(-r.amount, true)}</td>
                  {converged || !fc
                    ? <td className="num mut c-bal">·</td>
                    : <><td className="num mut hide-m">·</td>
                        <td className="num mut c-bal">·</td></>}
                </tr>
              ))}
              {fc && (
                <tr className="tr-today">
                  <td colSpan={converged ? 4 : 5}
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
                  <td className="mut c-date" style={{ whiteSpace: "nowrap", width: "1%" }}>{mmdd(r.date)}</td>
                  <td className={`c-label ${r.bal_now === null ? "mut" : ""}`}
                      style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.label}</td>
                  <td className={`num c-amt ${r.bal_now === null ? "mut" : r.amount > 0 ? "pos" : ""}`}
                      style={{ whiteSpace: "nowrap" }}>{money(r.amount)}</td>
                  {converged ? (
                    <td className={`num c-bal ${r.bal_stmt !== null && r.bal_stmt < 0 ? "neg" : "mut"}`}
                        style={{ whiteSpace: "nowrap" }}>{r.bal_stmt !== null ? money(r.bal_stmt) : "·"}</td>
                  ) : (
                    <>
                      <td className={`num hide-m ${r.bal_now !== null && r.bal_now < 0 ? "neg" : "mut"}`}
                          style={{ whiteSpace: "nowrap" }}>{r.bal_now !== null ? money(r.bal_now) : "·"}</td>
                      <td className={`num c-bal ${r.bal_stmt !== null && r.bal_stmt < 0 ? "neg" : "mut"}`}
                          style={{ whiteSpace: "nowrap" }}>{r.bal_stmt !== null ? money(r.bal_stmt) : "·"}</td>
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
