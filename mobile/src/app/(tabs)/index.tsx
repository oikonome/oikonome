// Today — the verdict, what's left to spend today, and the recent pane.
// Read-only mirror of the web Today page's core; every number is
// server-composed.
import AsyncStorage from "@react-native-async-storage/async-storage";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useScrollToTop } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Alert as RNAlert, Pressable, ScrollView, StyleSheet, Text, View,
         useWindowDimensions } from "react-native";
import PullRefresh from "../../components/pull-refresh";

import MoneyMap, { ledgerRoute, sentPlanRows, sentPlanTotal }
  from "../../components/money-map";
import SegmentedControl from "../../components/segmented-control";
import Sparkline from "../../components/sparkline";
import ForecastRecord from "../../components/forecast-record";
import { disclosure } from "../../lib/a11y";
import { FailedLine, StaleLine } from "../../components/loaded";
import TxnRow from "../../components/txn-row";
import { Meter } from "../../components/ui";
import { checklistOpen, checklistSteps } from "../../components/wizard-logic";
import { PrimaryCheckingPick } from "../../components/wizard-primary-checking";
import { consumeLaunch, launchSettled } from "../../lib/launch";
import { errText, setHandedOffTxn, type Txn,
         type TodayFull } from "../../lib/api";
import { patchQuery, saveSettings } from "../../lib/cache";
import { shiftYmd, ymd } from "../../lib/dates";
import { autopayMarks, dueMarks, noCheckingLinked,
         nearDay, pendingRecoveryLine, resolveFace } from "../../lib/pure";
import { useRefetchWhenShown } from "../../lib/focus";

const TODAY_KEY = ["today"];
import { useSession } from "../../lib/session";
import { hasLeftWizard } from "../../lib/storage";
import { heroLabel, VERDICT } from "../../lib/verdict";
import { useDemo, useOwner, useViewer } from "../../lib/viewer";
import { C, mmddyy, money } from "../../lib/theme";

// where a demo instance keeps the hero face, since it cannot keep it on
// the account (mirrors the web's oiko-today-face)
const FACE_KEY = "oikonome.todayFace";
// the most serious alert leads the needs-you line (the web's sevRank)
const SEV_RANK: Record<string, number> = { bad: 3, warn: 2, info: 1, good: 0 };
const sevRank = (sev: string) => SEV_RANK[sev] ?? 2;


export default function Today() {
  const { client, serverUrl } = useSession();
  // the household's default tab, applied ONCE per app launch: Today is
  // the navigator's home, so the first mount of this screen is the moment
  // the app "opens" — a later tap on the Today tab really means Today
  const router0 = useRouter();
  // subscribed to the one field it reads: a settings refetch elsewhere
  // must not repaint the whole Today screen
  const homeQ = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client,
    select: (v) => ({ home_mobile: v.home_mobile as string | undefined }) });
  useEffect(() => {
    if (!homeQ.isSuccess) return;
    // wait for the root layout to look for a launching notification
    // tap — the tap owns the launch, and it is only known after the
    // first render
    let live = true;
    launchSettled().then(() => {
      if (!live || !consumeLaunch()) return;
      const h = homeQ.data?.home_mobile;
      if (h && ["transactions", "bills", "accounts", "more"].includes(h))
        router0.replace(`/${h}` as never);
    });
    return () => { live = false; };
  }, [homeQ.isSuccess, homeQ.data?.home_mobile, router0]);
  const router = useRouter();
  const viewer = useViewer();
  const owner = useOwner();
  const demo = useDemo();
  const { width } = useWindowDimensions();
  const qc = useQueryClient();
  // back-nav: null = live today; a date = the report card for that day
  const [viewDate, setViewDate] = useState<string | null>(null);
  const q = useQuery({
    queryKey: viewDate ? ["today", viewDate] : ["today"],
    queryFn: () => client!.todayFull(viewDate ?? undefined),
    enabled: !!client,
    placeholderData: (prev) => prev,
  });
  useRefetchWhenShown(q, TODAY_KEY);
  // the home-screen widget shows this hero's simple face; when the live
  // Today payload settles and a widget is placed, refresh it so opening
  // the app never leaves the launcher showing an older verdict. The
  // refresh costs the server a second Today computation, so it is skipped
  // when no widget is placed and spaced out when that cannot be known.
  useEffect(() => {
    if (!client || viewDate || !q.data || q.isFetching) return;
    const stamp = JSON.stringify(q.data);
    import("../../lib/glance").then(({ refreshWidgetIfPlaced }) =>
      refreshWidgetIfPlaced(client, stamp)).catch(() => {});
  }, [client, viewDate, q.data, q.isFetching]);
  const ob = useQuery({
    queryKey: ["onboarding"],
    queryFn: () => client!.onboarding(),
    enabled: !!client,
    staleTime: 5 * 60_000,
  });

  const d = q.data;
  // the month on view, for the ledger a label opens (web: Today.tsx tileTo)
  // asOf: the day on view — a past day's tiles are that day's figures, so
  // the rows a label opens stop on that day too
  const period = d ? { y: Number(String(d.date).slice(0, 4)), m: Number(String(d.date).slice(5, 7)),
                       asOf: String(d.date).slice(0, 10) }
                   : { y: new Date().getFullYear(), m: new Date().getMonth() + 1 };
  // The tiles are plan buckets, so the link names the bucket, not a
  // category: the Food allowance also counts store rows overridden to
  // food, a carve-out is a plan label no transaction carries, and
  // "Everything else" is the leftover. The server answers a bucket with
  // exactly the rows its number counted.
  const tileTo = (label: string) =>
    `bucket=${encodeURIComponent(
       label === "Food" ? "food"
       : label === "Everything else" ? "other" : label)
     }&y=${period.y}&m=${period.m}${
       "asOf" in period ? `&as_of=${period.asOf}` : ""}`;
  // re-tapping the active tab scrolls back to the top
  const topRef = useRef<ScrollView>(null);
  useScrollToTop(topRef);
  const [allEvents, setAllEvents] = useState(false);
  // the forecast's explanatory sentences, folded away until asked for
  const [aboutOpen, setAboutOpen] = useState(false);
  // the card scenario the cash chart and timeline show (the web's
  // selector); "usual" is the server's headline and the default
  const [scenKey, setScenKey] = useState<string>("usual");
  // the nearly-done setup list is folded until asked for
  const [setupAll, setSetupAll] = useState(false);
  // hero face override while the settings write lands — the server's
  // today_view is the durable value the web follows too; the daily
  // email's face is its own setting (email_schedule.daily.summary)
  const [faceOverride, setFaceOverride] =
    useState<"summary" | "detail" | null>(null);
  // A demo refuses every settings write (shared printed login), so the
  // face is kept per-device there instead of snapping back on a 403 —
  // and it PERSISTS, the way the web's localStorage copy does: the choice
  // surviving on one client and evaporating on the other is exactly the
  // kind of drift the web/mobile parity rule exists to stop.
  const [perDevice, setPerDevice] =
    useState<"summary" | "detail" | null>(null);
  useEffect(() => {
    if (!demo) return;
    AsyncStorage.getItem(FACE_KEY)
      .then((v) => setPerDevice(v === "detail" || v === "summary" ? v : null))
      .catch(() => { /* no storage — the choice lasts the session */ });
  }, [demo]);
  // a cached payload from an older server/app has no `simple` — render
  // the detail rows it does carry rather than read into undefined.
  // The stored face is the owner's own per-device choice, so a viewer
  // still sees the household's saved one (the web reads it back the same
  // way, and hides the toggle from viewers on both clients).
  const adv = resolveFace({ override: faceOverride, perDevice,
                            saved: d?.today_view, demo, viewer }) === "detail"
    || (!!d && !d.simple);
  // the hero's month figure (web: Today.tsx heroBudget/remaining)
  const heroBudget = d ? d.variable_budget_paced ?? d.variable_budget : 0;
  // what is left of it is the server's alone (month_remaining_paced); an
  // older server sends none and the line is left out
  const heroRemaining = d?.month_remaining_paced;
  const setFace = (f: "summary" | "detail") => {
    setFaceOverride(f);                       // instant; the write follows
    if (demo) {
      setPerDevice(f);
      AsyncStorage.setItem(FACE_KEY, f).catch(() => { /* best effort */ });
      return;
    }
    if (!client) return;
    // the reply seeds ["settings"]; the cached Today learns the face it
    // just wrote instead of refetching the heaviest payload to hear it
    saveSettings(qc, client, { today_view: f })
      .then(() => patchQuery<TodayFull>(qc, ["today"], { today_view: f }))
      .catch((e) => {
        // failed write ≠ new truth — and a silent snap-back reads as a
        // dead tap, so say what happened (the web toasts it)
        setFaceOverride(null);
        RNAlert.alert("Couldn't save the view", errText(e));
      });
  };
  const [whyOpen, setWhyOpen] = useState(false);
  // top-pane meters are DAY-scoped, the same rule as the web: today's
  // spend against today's allowance — the month gauges live in the
  // Monthly budget card. Totalled across buckets for the simple face.
  // the day bar is the server's (todayview.simple_day_meter), the same
  // bar the web and the email draw
  const daySpent = d?.day_meter?.spent ?? 0;
  const dayAllow = d?.day_meter?.allow ?? 0;
  const dayFrac = (spent: number, allow: number) =>
    allow > 0 ? Math.min(1, spent / allow) : spent > 0.005 ? 1 : 0;
  const shiftDay = (dir: -1 | 1) => {
    if (!d) return;
    // local-date math throughout — a UTC round trip here makes ‹ skip a
    // day east of Greenwich and › stick on the current view
    const next = shiftYmd(d.date, dir);
    const today = d.real_today ?? ymd(new Date());
    if (next >= today) setViewDate(null);
    else if (!d.min_date || next >= d.min_date) setViewDate(next);
  };
  // setup incomplete → the checklist card. OWNER only — the wizard
  // connects banks and configures the household, so a member's button
  // would lead to a page that turns them away (web: useSetupPending)
  const setupOpen = owner && ob.data && checklistOpen(ob.data);

  // A new account lands IN the guided setup, not on an empty dashboard —
  // the web's rule (App.tsx: wantsWizard). Signup and login both end up
  // here, so without this the wizard is reachable only by hunting through
  // More. Gated on `ob.data` so nothing redirects before onboarding state
  // loads, and on the per-tenant "left on purpose" flag so the wizard's
  // own exits are not bounced straight back in.
  // The flag is keyed by TENANT, as the web keys it — keyed by server
  // URL it would outlive the account: a household that left the wizard,
  // erased itself and signed up again on the same phone would land on
  // Home with no Setup, while a fresh install and the web offer it.
  // Nothing is read until the tenant id is known.
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
                        enabled: !!client, staleTime: 5 * 60_000,
                        select: (m) => m.tenant_id });
  const tenantId = me.data;
  // a two-factor recovery pending on this account: the push that announced
  // it carries no text, so Home says it too and links to the cancel card
  const recoveryQ = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
                               enabled: !!client, staleTime: 5 * 60_000,
                               select: (m) => m.factor_reset_pending });
  const recovery = pendingRecoveryLine(recoveryQ.data, mmddyy);
  const [leftWiz, setLeftWiz] = useState<boolean | null>(null);
  useEffect(() => {
    if (!tenantId) return;
    let live = true;
    hasLeftWizard(tenantId).then((v) => { if (live) setLeftWiz(v); });
    return () => { live = false; };
  }, [tenantId]);
  // The redirect is the OWNER's alone, as on the web: the wizard connects
  // banks and configures the instance, so a member sent there would land
  // on a page that only tells them to ask someone else.
  const wantsWizard = owner && !!setupOpen && !ob.data?.wizard_done
    && leftWiz === false;
  // once per mount: every onboarding refetch that flips this false→true
  // (a step marked done invalidates it) would otherwise REPLACE the
  // wizard route again, remounting the wizard mid-run and dropping it
  // back to step 1
  const sentToWizard = useRef(false);
  useEffect(() => {
    if (wantsWizard && !sentToWizard.current) {
      sentToWizard.current = true;
      router.replace("/welcome" as never);
    }
  }, [wantsWizard, router]);

  // ---- cash: quiet until it matters — the web hero's three states,
  // same math (autopay-statement is the realistic trajectory) ----
  // no checking account: cash ahead is unknown, not $0 — the forecast's
  // walk starts from nothing, so it is not shown and no alarm is drawn
  // from it (the web's rule, and the email's)
  const noChecking = !!d && noCheckingLinked(d.runway);
  const fc = noChecking ? null : d?.forecast ?? null;
  const p0 = fc?.pace_stmt ?? null;
  // the notice the server decided (todayview: state, the dollar it
  // quotes, the date it runs to) — the web's and the email's
  const note = noChecking ? null : (d?.cash_notice ?? null);

  // ---- cash timeline: paid ✓ rows arrive in forecast_rows with no
  // balance; future events show through the low point by default ----
  // the three card scenarios as sent (your usual, pay all cards now, pay
  // on the due date), each with its walk, low point in words, chart lines
  // and rows; null from an older server
  const scens = fc && fc.scenarios !== undefined ? (fc.scenarios ?? []) : null;
  const scen = scens?.find((x) => x.key === scenKey) ?? scens?.[0] ?? null;
  const { paidRows, futureRows } = useMemo(() => {
    const all = d?.forecast_rows ?? [];
    const paid = new Set(all.filter(
      (r) => r.bal_now === null && r.bal_stmt === null && r.date <= d!.date));
    // the selected scenario's rows after today; an older server's
    // forecast_rows otherwise (older-server path: stands in for `scenarios`)
    const future = scen
      ? scen.rows.map((r) => ({ date: r.date, amount: r.amount ?? 0,
                                label: r.label, bal_now: r.balance,
                                bal_stmt: r.balance, kind: r.kind,
                                card: r.card ?? null }))
      : all.filter((r) => !paid.has(r));
    return { paidRows: [...paid], futureRows: future };
  }, [d, scen]);
  const openTxn = useCallback((x: Txn) => {
    setHandedOffTxn(x);
    router.push({ pathname: "/txn", params: { id: x.id } });
  }, [router]);
  // the month-end row always shows: the tile above quotes its balance
  const lowDate = (scen ?? p0)?.min_date ?? null;
  const preLow = lowDate
    ? futureRows.filter((r) => r.kind === "month_end"
                        || r.date <= lowDate) : futureRows;
  const shownFuture = allEvents || preLow.length === 0
    ? futureRows : preLow;
  // the recent window's variable spending: the server's sum, the
  // figure the email prints
  const recentSum = d?.recent_variable_spending ?? 0;
  // dates carry the year, like the web's mmdd() — a truncated 08/10
  // reads wrong the moment a forecast or ETA crosses New Year
  const mmdd = mmddyy;
  // the footnote's two kinds of missed bill: never posted, and posted then
  // withdrawn by the bank feed (the fourth slot; an older server omits it)
  const lateBills = (d?.overdue_unpaid ?? []).filter((o) => !o[3]);
  const goneBills = (d?.overdue_unpaid ?? []).filter((o) => !!o[3]);
  // the web's converged rule: the two card scenarios collapse to one
  // line only when min AND min_date agree (same trough on a different
  // day is still two stories)
  const lows = fc ? [
    { label: "pay all cards now", short: "cards now", s: fc.pace_now },
    { label: "autopay statement", short: "autopay", s: fc.pace_stmt },
  ] : [];
  const converged = lows.length > 0 &&
    lows.every((l) => l.s.min === lows[0].s.min
                      && l.s.min_date === lows[0].s.min_date);
  const yesterday = d ? shiftYmd(d.date, -1) : "";

  return (
    <ScrollView
      ref={topRef}
      style={s.wrap}
      contentContainerStyle={s.content}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => q.refetch()} />}>
      <StaleLine query={q} />
      {recovery && (
        <Pressable style={s.card} accessibilityRole="link"
                   onPress={() => router.push("/security" as never)}>
          <Text style={{ color: C.warn, fontSize: 13 }}>{recovery} ›</Text>
        </Pressable>
      )}
      {!d && !q.isError && <Text style={s.mut}>Loading…</Text>}
      {q.isError && !d && (
        <FailedLine q={q} what="Today" />
      )}
      {d && (
        <>
          {/* past-day banner — cash forecast and alerts describe NOW,
              so the server omits them for a past date */}
          {!d.is_today && (
            <View style={[s.alert, { borderColor: C.accent }]}>
              <Text style={{ color: C.mut, fontSize: 13, flex: 1 }}>
                Viewing a past day — {mmdd(d.date)}. Cash forecast and
                alerts are hidden (they describe now, not this day).
              </Text>
              <Text style={{ color: C.accent, fontSize: 13,
                             fontWeight: "700" }}
                    onPress={() => setViewDate(null)}>
                ‹ today
              </Text>
            </View>
          )}
          {/* While the guided wizard is unfinished it is THE setup path,
              so Today offers exactly one door into it — the web's rule
              verbatim: "a parallel checklist whose links bypass it just
              competes". The checklist returns once the wizard is done or
              dismissed. */}
          {/* an unread setup state is not "setup finished": the failed
              line sits where the setup card would */}
          {d.is_today && owner && ob.data === undefined && ob.isError && (
            <FailedLine q={ob} what="the setup progress" />)}
          {d.is_today && setupOpen && !ob.data!.wizard_done && (
            <View style={s.card}>
              <Text style={s.h2}>Welcome — let&apos;s finish setting up</Text>
              <Text style={s.mut}>
                The guided setup picks up right where you left off —
                connect accounts, bring in history, confirm bills, set
                budgets.
              </Text>
              <Pressable accessibilityRole="button" style={s.btn}
                         onPress={() => router.push("/welcome" as never)}>
                <Text style={s.btnText}>Resume setup →</Text>
              </Pressable>
            </View>
          )}
          {d.is_today && setupOpen && ob.data!.wizard_done && (() => {
            const steps = checklistSteps(ob.data!);
            const left = steps.filter((x) => !x.done);
            // nearly done, the list is mostly ticked rows above the
            // verdict: it folds to ONE slim line naming what is left —
            // the needs-you anatomy, like the web's Onboarding row
            if (setupAll || left.length === 0 || left.length > 2) return null;
            return (
              <View style={[s.needsRow, { borderLeftColor: C.accent }]}>
                <Text style={{ color: C.text, fontSize: 13,
                               fontWeight: "700" }}>
                  {left.length === 1 ? "1 setup step left"
                    : "2 setup steps left"}
                </Text>
                <Text numberOfLines={1}
                      style={{ color: C.mut, fontSize: 13, flex: 1 }}>
                  {left.map((x, i) => (
                    <Text key={x.key} style={{ color: C.accent }}
                          onPress={x.key === "checking"
                            ? () => setSetupAll(true)
                            : () => router.push(x.href as never)}>
                      {i > 0 ? " · " : ""}{x.label}
                    </Text>
                  ))}
                </Text>
                <Text style={{ color: C.mut, fontSize: 13 }}
                      onPress={() => setSetupAll(true)}>
                  show all
                </Text>
              </View>
            );
          })()}
          {d.is_today && setupOpen && ob.data!.wizard_done && (() => {
            const steps = checklistSteps(ob.data!);
            const left = steps.filter((x) => !x.done);
            if (!setupAll && left.length > 0 && left.length <= 2) return null;
            return (
            <View style={s.card}>
              <Text style={s.h2}>Welcome — let&apos;s finish setting up</Text>
              {(() => {
                const done = steps.filter((x) => x.done).length;
                return (
                  <>
                    <Text style={s.mut}>
                      {done}/{steps.length} done. Every step is reversible.
                    </Text>
                    {steps.map((x) => (
                      <View key={x.key}>
                        <Text style={{ color: x.done ? C.mut : C.accent,
                                       fontSize: 13, paddingVertical: 2,
                                       textDecorationLine: x.done
                                         ? "line-through" : "none" }}
                              accessibilityRole={x.done ? "text" : "link"}
                              accessibilityLabel={x.done ? `${x.label}, done`
                                : x.label}
                              onPress={x.done ? undefined
                                : () => router.push(x.href as never)}>
                          {x.done ? "☑" : "☐"} {x.label}
                        </Text>
                        {/* the choice is made here, on the tap — the
                            Accounts edit sheet is the long way round */}
                        {!x.done && x.key === "checking" && (
                          <PrimaryCheckingPick compact
                            candidates={ob.data!.checking_candidates ?? []}
                            pinned={ob.data!.primary_checking_id ?? null} />
                        )}
                      </View>
                    ))}
                  </>
                );
              })()}
              {ob.data!.pending_proposals > 0 && (
                <Text style={{ color: C.warn, fontSize: 12 }}>
                  {ob.data!.pending_proposals} detected bill
                  {ob.data!.pending_proposals === 1 ? "" : "s"} await
                  review on Bills.
                </Text>
              )}
            </View>
            );
          })()}
          {/* what needs you, as ONE line (the web's needs-you row): how
              many alerts wait and the most serious one, opening Alerts —
              where each is fixed or dismissed with the same verbs. A bar
              per alert pushed the day's number off the first screen. */}
          {(d.alerts ?? []).length > 0 && (() => {
            const worst = [...d.alerts].sort((x, y) =>
              sevRank(y.severity) - sevRank(x.severity))[0];
            const n = d.alerts.length;
            const tone = worst.severity === "bad" ? C.bad
              : worst.severity === "good" ? C.good
              : worst.severity === "info" ? C.accent : C.warn;
            return (
              <Pressable accessibilityRole="link"
                         onPress={() => router.push("/alerts" as never)}
                         style={[s.needsRow, { borderLeftColor: tone }]}>
                <Text style={{ color: C.text, fontSize: 13,
                               fontWeight: "700" }}>
                  {n === 1 ? "1 thing needs you" : `${n} things need you`}
                </Text>
                {/* two lines: one cut the amount and the ask off the end */}
                <Text numberOfLines={2}
                      style={{ color: C.mut, fontSize: 13, flex: 1 }}>
                  {worst.message}
                </Text>
                <Text style={{ color: C.accent, fontSize: 16 }}>›</Text>
              </Pressable>
            );
          })()}
          {/* the face toggle sits OUTSIDE the cards and scopes the WHOLE
              page, the same rule as the web: summary = the verdict card
              IS the page; detail brings back the plan line, goals, money
              map and cash timeline. Remembered server-side — web follows
              the same choice — except on a demo, which refuses settings
              writes, where it is remembered per device; the daily email's
              face is set separately in
              notification settings. OWNER only, like the web: the save
              goes through /api/settings, which 403s a viewer. */}
          {!viewer && (
          <View style={{ flexDirection: "row", justifyContent: "flex-end",
                         alignItems: "center", gap: 8,
                         marginHorizontal: 12, marginTop: 10 }}>
            <Text style={{ color: C.mut, fontSize: 12.5 }}>View</Text>
            <View style={{ flexDirection: "row", borderColor: C.border,
                           borderWidth: 1, borderRadius: 999,
                           overflow: "hidden" }}>
              {(["summary", "detail"] as const).map((f) => {
                const on = (adv ? "detail" : "summary") === f;
                return (
                  <Text key={f}
                        onPress={() => setFace(f)}
                        style={{ paddingHorizontal: 14, paddingVertical: 5,
                                 fontSize: 12.5,
                                 backgroundColor: on ? C.hover : undefined,
                                 color: on ? C.text : C.mut,
                                 fontWeight: on ? "600" : "400" }}>
                    {f === "summary" ? "Summary" : "Detail"}
                  </Text>
                );
              })}
            </View>
          </View>
          )}
          {/* Hidden entirely until budgets exist, like the web hero:
              with no plan the engine still produces a
              consistent "On budget · $0 of $0 left · $0 to spend today",
              which is arithmetic, not news, and reads as a verdict on a
              plan the household has not made. Loading onboarding data
              keeps the hero so it never flashes away. */}
          {!(ob.data && !ob.data.budgets_set) && (<>
          {/* THE hero card, mirroring the web's anatomy: the colored
              verdict sentence with the pace phrase, the month total
              demoted to context, then the star — LEFT TO SPEND TODAY
              rows with the big number stacked left, today sub-line, and
              the DAY-scoped meter (the month gauges live in the Monthly
              budget card). */}
          <View style={s.card}>
            <View style={{ flexDirection: "row", alignItems: "center" }}>
              <Text style={[s.verdict, { flex: 1,
                            color: VERDICT[d.verdict]?.color ?? C.text }]}>
                {heroLabel(d.verdict)}
              </Text>
              {/* day back-nav — any past day is a final report card;
                  dimmed at the ledger floor like the web's disabled ‹ */}
              {(() => {
                const atFloor = !!d.min_date && d.date <= d.min_date;
                return (
                  <Text style={{ color: C.mut, fontSize: 20,
                                 paddingHorizontal: 8,
                                 opacity: atFloor ? 0.3 : 1 }}
                        accessibilityRole="button" accessibilityLabel="Previous day"
                        onPress={atFloor ? undefined : () => shiftDay(-1)}>
                    ‹
                  </Text>
                );
              })()}
              {!d.is_today && (
                <Text style={{ color: C.accent, fontSize: 20,
                               paddingHorizontal: 8 }}
                      accessibilityRole="button" accessibilityLabel="Next day"
                      onPress={() => shiftDay(1)}>
                  ›
                </Text>
              )}
            </View>
            {d.pace_line ? (
              <Text style={{ color: VERDICT[d.verdict]?.color ?? C.mut,
                             fontSize: 14, fontWeight: "600" }}>
                {d.pace_line}
              </Text>
            ) : null}
            {heroRemaining != null && (
            <Text style={s.mut} accessibilityRole="link"
                  onPress={() => router.push("/budget" as never)}>
              {/* the month's one figure on Today opens Budget, where its
                  bars and the plan live (the web's link);
                  whole dollars, like the web hero and the daily email;
                  the month's spendable share and what is left of it,
                  composed once on the server */}
              <Text style={heroRemaining < 0
                             ? { color: C.bad } : undefined}>
                {money(Math.abs(heroRemaining), false)}
              </Text>
              {d.month_words != null
                ? ` ${d.month_words}`
                // older-server path: stands in for `month_words`
                : (heroRemaining < 0
                    ? ` over the ${money(heroBudget, false)}`
                    : ` of ${money(heroBudget, false)} left`)
                  + ` · ${d.days_left} day${d.days_left === 1 ? "" : "s"}`}
              {" ›"}
            </Text>)}

            <Text style={s.starLbl}>
              {/* older-server path: stands in for `left_label` */}
              {d.left_label ?? "Left to spend today"}</Text>
            {!adv && (
              <View>
                {/* SIMPLE face (the default): the one number, a DAY meter
                    (no pace tick — the day is the unit), and a chip per
                    bucket — strings composed by the server (d.simple),
                    identical on web + email */}
                <Text style={[s.tileBig, { fontSize: 40 }]}>
                  {money(d.simple.left_today, false)}
                </Text>
                <Meter big frac={dayFrac(daySpent, dayAllow)}
                       tone={daySpent > dayAllow + 0.5 ? "bad" : undefined} />
                <View style={{ flexDirection: "row", flexWrap: "wrap",
                               gap: 6, marginTop: 8 }}>
                  {d.simple.chips.map((c) => (
                    <Text key={c.text}
                          style={{ color: c.tone === "neg" ? C.bad : C.text,
                                   backgroundColor: C.hover,
                                   borderColor: C.border, borderWidth: 1,
                                   borderRadius: 999, overflow: "hidden",
                                   paddingHorizontal: 10,
                                   paddingVertical: 2, fontSize: 12 }}>
                      {c.text}
                    </Text>
                  ))}
                </View>
                {d.sweep_line ? (
                  <Text style={[s.mut, { fontSize: 12, marginTop: 6 }]}>
                    {d.sweep_line}
                  </Text>
                ) : null}
              </View>
            )}
            {adv && (
            <View>
              {/* the web's fixed pair, in its order — never key iteration,
                  which follows payload key order and can leak engine keys.
                  Map-style ROWS, not tiles — full-width bars, one idiom
                  with the money map card */}
              {[
                ["Food", d.allow.food] as const,
                ["Everything else", d.allow.other] as const,
                ...(d.allow_kids ?? []).map((k) => [k.name, k] as const),
              ].filter(([, a]) => a && !(a as { empty?: boolean }).empty)
               .map(([name, a]) => {
                const overT = a.left_today < -0.5;
                // nothing to hand out and nothing spent (server-decided):
                // ONE line, as on the web and in the email
                if (a.zero_day && !overT) return (
                  <Text key={name} style={{ marginTop: 10, color: C.mut,
                                            fontSize: 12, lineHeight: 18 }}>
                    <Text style={{ color: C.text, fontWeight: "700",
                                   fontSize: 13 }}
                          onPress={() => router.push(ledgerRoute(tileTo(name)) as never)}>
                      {name}</Text>
                    {" $0 today"}
                    {a.daily_note ? (<>
                      {" · "}
                      <Text style={{ color: a.rate < a.plan ? C.bad : C.good }}>
                        {a.daily_note}</Text>
                    </>) : null}
                    {a.recover_note ? ` · ${a.recover_note}` : ""}
                  </Text>
                );
                return (
                  <View key={name} style={{ marginTop: 10 }}>
                    {/* one line a budget — its name, what is left today —
                        and its bar under it, so every budget fits one
                        screen. The label opens the rows behind the number:
                        the category's ledger for the month on view */}
                    <View style={{ flexDirection: "row", alignItems: "center",
                                   justifyContent: "space-between", gap: 8 }}>
                      <Pressable accessibilityRole="button"
                                 onPress={() => router.push(ledgerRoute(tileTo(name)) as never)}
                                 style={[s.lblPress, { flexShrink: 1 }]}>
                        {({ pressed }) => (
                          <Text style={[s.h2, pressed && { color: C.accent }]}>{name}</Text>)}
                      </Pressable>
                      <Text style={{ color: overT ? C.bad : C.text,
                                     fontSize: 17, fontWeight: "800",
                                     fontVariant: ["tabular-nums"] }}
                            accessibilityLabel={overT
                              ? `over today by ${money(-a.left_today, false)}`
                              : `${money(a.left_dollars
                                  ?? Math.max(0, a.left_today), false)} left of ${
                                  money(a.today_allowance_dollars
                                    ?? a.today_allowance, false)} today`}>
                        {/* the server's whole dollars (floored once, the
                            figure the recovery sentence and the SMS say);
                            an older server sends only the cents */}
                        {overT ? `over by ${money(-a.left_today, false)}`
                          : money(a.left_dollars
                              ?? Math.max(0, a.left_today), false)}
                      </Text>
                    </View>
                    {/* the line under the figure — the server's words, as
                        on the web at every width and in the email */}
                    {a.day_line ? (
                      <Text style={{ color: overT ? C.bad : C.mut,
                                     fontSize: 11 }}>
                        {a.day_line}
                      </Text>
                    ) : null}
                    <Meter frac={dayFrac(a.spent_today, a.today_allowance)}
                           tone={overT ? "bad" : undefined} />
                    {a.daily_note ? (
                      <Text style={{ color: a.rate < a.plan ? C.bad : C.good,
                                     fontSize: 11 }}>
                        {a.daily_note}
                      </Text>
                    ) : null}
                    {/* how many no-spend days bring the reduced daily
                        back — the web's line under the note */}
                    {a.recover_note ? (
                      <Text style={{ color: C.mut, fontSize: 11 }}>
                        {a.recover_note}
                      </Text>
                    ) : null}
                  </View>
                );
              })}
            </View>
            )}

            {/* pinned bills live INSIDE the hero, under the tiles —
                the web's anatomy: they answer the same "what's left"
                question the tiles do */}
            {(d.bill_cards ?? []).length > 0 && (
              <>
                <Text style={s.starLbl}>Pinned</Text>
                {d.bill_cards.map((b) => (
                  <View key={b.label} style={{ gap: 2, marginTop: 6 }}>
                    {/* number under the label, left — the pane's one
                        anatomy */}
                    {/* the label opens the bill's history */}
                    <Pressable accessibilityRole="button"
                               onPress={() => router.push({ pathname: "/bill-history",
                                                            params: { payee: b.label } } as never)}
                               style={s.lblPress}>
                      {({ pressed }) => (
                        <Text style={[s.h2, pressed && { color: C.accent }]}>{b.label}</Text>)}
                    </Pressable>
                    <Text style={[s.tileBig,
                                  { color: b.over ? C.bad : C.text }]}>
                      {money(b.left, false)}
                    </Text>
                    <Text style={s.mut}>
                      {b.kind === "envelope" ? "left · " : "to go · "}{b.sub}
                    </Text>
                    <Meter big frac={b.frac} pace={b.pace_frac}
                           tone={b.over ? "bad" : undefined} />
                    {b.status ? (
                      <Text style={{ color: b.status_tone === "neg" ? C.bad
                                       : b.status_tone === "pos" ? C.good
                                       : b.status_tone === "warn" ? C.warn
                                       : C.mut, fontSize: 12 }}>
                        {b.status}
                      </Text>
                    ) : null}
                  </View>
                ))}
              </>
            )}

            {d.variable_scale && (
              <Text style={{ color: C.warn, fontSize: 12, marginTop: 6 }}>
                Variable budgets auto-scaled from{" "}
                {money(d.variable_scale.static_total, false)} to fit this
                month&apos;s bills.
              </Text>
            )}
            {d.runway?.balance_unreliable ? (
              <Text style={[s.watch, { color: C.text }]}>
                <Text style={{ fontWeight: "700" }}>
                  Cash forecast is waiting on your balance
                </Text>
                {" — imported accounts start at $0. "}
                <Text style={{ color: C.accent }}
                      onPress={() => router.push("/accounts" as never)}>
                  Set today&apos;s checking balance on Accounts
                </Text>
                {" and everything here comes alive."}
              </Text>
            ) : noChecking ? (
              <Text style={[s.watch, { color: C.text }]}>
                <Text style={{ fontWeight: "700" }}>
                  No checking account linked
                </Text>
                {" — cash ahead starts from your checking balance, so "
                  + "there is nothing to forecast yet. "}
                <Text style={{ color: C.accent }}
                      onPress={() => router.push("/accounts" as never)}>
                  Link your checking account on Accounts
                </Text>
                {" to see cash ahead."}
              </Text>
            ) : note?.state && note.ask ? (
              <Text style={[s.watch, { color: C.accent }]}
                    onPress={() => router.push("/bills" as never)}>
                {note.ask}
              </Text>
            ) : note?.state && note.text ? (
              // the sentence is the server's (todayview.cash_sentence),
              // the web's and the email's; a server from before it was
              // sent shows no sentence rather than this screen writing one
              <Text style={[s.watch,
                            { color: note.state === "critical" ? C.bad : C.warn }]}>
                {`● ${note.text}`}
              </Text>
            ) : !noChecking && d.cash_band ? (
              // the quiet heavier-month line, the web's and the email's
              <Text style={[s.watch, { color: C.mut }]}>
                {`● ${d.cash_band.text}`}
              </Text>
            ) : null}

            {(d.why_chips ?? []).length > 0 && (
              <View style={{ marginTop: 8, gap: 4 }}>
                <View style={{ flexDirection: "row", flexWrap: "wrap",
                               gap: 6, alignItems: "center" }}>
                  <Text style={s.mut}>Driving it:</Text>
                  {d.why_chips!.map((c) => (
                    <View key={c.payee} style={s.chip}>
                      <Text style={{ color: C.text, fontSize: 12 }}
                            numberOfLines={1}>
                        {c.payee}{" "}
                        <Text style={{ color: C.bad, fontWeight: "700" }}>
                          {money(c.amount, false)}
                        </Text>
                        {c.count > 1 ? (
                          <Text style={{ color: C.mut }}> {c.count}×</Text>
                        ) : null}
                      </Text>
                    </View>
                  ))}
                </View>
                {d.why && d.why.entries.length > 0 && (
                  <>
                    <Text style={{ color: C.accent, fontSize: 12 }}
                          onPress={() => setWhyOpen((x) => !x)}>
                      by category {whyOpen ? "▾" : "▸"}
                    </Text>
                    {whyOpen && d.why.entries.map((e) => (
                      <Text key={e.name} style={{ fontSize: 13,
                                                  lineHeight: 19 }}>
                        {/* the line's bucket is a tile's bucket: same door */}
                        <Text style={{ color: C.text, fontWeight: "700" }}
                              onPress={() => router.push(
                                ledgerRoute(tileTo(e.name)) as never)}>
                          {e.name}{" "}
                        </Text>
                        <Text style={{ color: e.tone === "over" ? C.bad
                                         : e.tone === "under" ? C.good
                                         : C.mut }}>
                          {e.text}
                        </Text>
                      </Text>
                    ))}
                    {whyOpen && d.why.recovery ? (
                      <Text style={{ color: C.warn, fontSize: 13,
                                     lineHeight: 19 }}>
                        {d.why.recovery}
                      </Text>
                    ) : null}
                  </>
                )}
              </View>
            )}

            {adv && d.plan_line && d.buckets && (() => {
              const pl = d.plan_line;
              return (
              <Text style={[s.mut, { marginTop: 8, borderTopColor: C.border,
                                     borderTopWidth: StyleSheet.hairlineWidth,
                                     paddingTop: 8 }]}>
                {/* the terms in the whole dollars the server rounded to
                    the printed excess (plan_line), so the line adds up as
                    printed; an older server that sends none shows no line
                    rather than this screen picking other figures */}
                Plan: <B>{money(pl.income, false)}</B> in →{" "}
                {/* the plan's own figures, the basis the excess is
                    computed from (same figures and words as web) */}
                <B>{money(pl.bills, false)}</B>{" "}
                {/* "bills & envelopes" with a pool; older-server path:
                    stands in for `plan_bills_label` — "bills" */}
                {d.plan_bills_label ?? "bills"}
                {/* this month against the average — the server's words */}
                {d.bills_weight ? ` (${d.bills_weight})` : ""} →{" "}
                <B>{money(pl.to_spend, false)}</B> to spend →{" "}
                {pl.saved !== null ? (
                  <>
                    <B>{money(pl.saved, false)}</B> saved →{" "}
                  </>
                ) : null}
                {/* the excess in the Budget screen's words and tone
                    (todayview.plan_excess_words) */}
                <Text style={{ color: d.plan_excess_words !== undefined
                                 && d.plan_excess_words !== null
                                 ? (d.plan_excess_words.tone === "neg" ? C.bad
                                    : d.plan_excess_words.tone === "mut" ? C.mut
                                    : C.good)
                                 // older-server path: stands in for `plan_excess_words`
                                 : pl.excess >= 0 ? C.good : C.bad,
                               fontWeight: "700" }}>
                  {money(pl.excess, false)}
                </Text> {d.plan_excess_words !== undefined
                  ? (d.plan_excess_words?.text ?? "excess")
                  // older-server path: stands in for `plan_excess_words`
                  : "excess"}
                {/* sweep goals sit beside the chain, not inside it — the
                    sentence is server-composed so no surface can drift */}
                {d.sweep_line ? <> · {d.sweep_line}</> : null}
                {"   "}
                <Text style={{ color: C.accent, fontWeight: "400" }}
                      onPress={() => router.push("/budget" as never)}>
                  balance the budget →
                </Text>
              </Text>
              );
            })()}
          </View>
          </>)}

          {/* only when a TRACKABLE goal exists, or one that needs an
              account to become trackable — a card of plan-only zero rows
              is chrome, not information (the web's filter) */}
          {adv && (d.savings_goals ?? []).some(
              (g) => !g.plan_only || g.needs_account) && (
            <View style={s.card}>
              <Text style={s.h2}>Savings goals</Text>
              {d.savings_goals.filter((g) =>
                  !(g.plan_only && !g.monthly_plan && !g.target)).map((g) => {
                // the web's pace phrase, verbatim priority order — and the
                // web's gate: plan_only alone (a null saved with a real
                // destination is "no contributions", not "plan only")
                const pace = g.needs_account
                  ? "match words need an account — pick one on the "
                    + "Budget page"
                  : g.plan_only
                  ? "plan only — set a destination account on the "
                    + "Budget page to track progress"
                  : g.saved === null
                    ? "no contributions in 3 months"
                  : !g.target
                    ? ((g.saved_90d ?? 0) > 0
                        ? `${money(g.saved_90d!, false)} in the last 3 months`
                        // a server from before the window net was sent
                        : g.saved_90d === undefined && (g.rate_90d ?? 0) > 0
                        ? `saving ${money(g.rate_90d!, false)}/mo`
                        // withdrawals outweighing deposits is not the
                        // same news as nothing arriving
                        : g.contributed_90d
                          ? "no net saving in the last 3 months"
                          : "no contributions in 3 months")
                  : g.pct != null && g.pct >= 100 ? "funded ✓"
                  : g.on_pace === false
                    ? `off pace for ${mmdd(g.target_date!)}`
                  : g.on_pace ? `on pace for ${mmdd(g.target_date!)}`
                  : g.eta ? `~${mmdd(g.eta)} at the current rate`
                  : g.contributed_90d
                    ? "no net saving in the last 3 months"
                  : "no contributions in 3 months";
                const paceColor = g.on_pace === false ? C.bad
                  : g.plan_only || g.saved === null ? C.mut
                  : g.pct != null && g.pct >= 100 ? C.good : C.mut;
                return (
                <View key={g.name} style={{ gap: 2, marginTop: 4 }}>
                  <View style={s.row}>
                    <Text style={{ color: C.text, fontSize: 14,
                                   fontWeight: "600" }}>{g.name}</Text>
                    <Text style={{ color: C.mut, fontSize: 13 }}>
                      {/* the web's figure: a goal with a destination
                          but nothing posted yet reads $0, not its plan */}
                      {g.plan_only
                        ? `plan ${money(g.monthly_plan, false)}/mo`
                        : g.target
                          ? `${money(g.saved ?? 0, false)} of ${
                              money(g.target, false)}${
                              g.pct != null ? ` (${g.pct}%)` : ""}`
                          : `${money(g.saved ?? 0, false)} saved`}
                    </Text>
                  </View>
                  <Text style={{ color: paceColor, fontSize: 12 }}>
                    {pace}
                  </Text>
                  {/* bar only with a target — pct is meaningless without
                      one (web rule); pct arrives 0-100, Meter wants 0-1 */}
                  {!g.plan_only && !!g.target && g.pct !== null && (
                    <Meter frac={Math.min(1, g.pct / 100)} />
                  )}
                </View>
                );
              })}
            </View>
          )}

          {/* ---- where it's going — the money map, bars only (its
               headline lives in the hero card; plan-only rows fold into
               the map's own footer line). DETAIL-ONLY, the same rule as
               the web: in summary the verdict card is the whole page. */}
          {adv && d.buckets && (() => {
            return (
            <View style={s.card}>
              <Text style={s.h2}>Monthly budget</Text>
              <MoneyMap
                // the server's rows (todayview._plan_rows), the web's and
                // the email's
                rows={sentPlanRows(d.plan_rows ?? [])}
                total={sentPlanTotal(d.plan_total)}
                footnote={((d.overdue_unpaid ?? []).length > 0
                           || (d.irregular_bills ?? []).length > 0) ? (
                  <Text style={{ color: C.mut, fontSize: 12, marginTop: 4,
                                 lineHeight: 17 }}>
                    {(d.irregular_bills ?? []).length > 0 && (
                      <>
                        Non-monthly bills this month:{" "}
                        {d.irregular_bills!.slice(0, 4).map((x, k) => (
                          <Text key={k}>
                            {x.payee} <B>{money(x.amount, false)}</B> ({x.label})
                            {k < Math.min(d.irregular_bills!.length, 4) - 1
                              ? " · " : ""}
                          </Text>
                        ))}
                      </>
                    )}
                    {(d.irregular_bills ?? []).length > 0
                      && lateBills.length > 0 && "\n"}
                    {lateBills.length > 0 && (
                      <>
                        Scheduled but not yet posted:{" "}
                        {lateBills.slice(0, 4).map(([p, a, due], k) => (
                          <Text key={k}>
                            {p} {money(a, false)} (due {mmdd(due)})
                            {k < Math.min(lateBills.length, 4) - 1
                              ? ", " : ""}
                          </Text>
                        ))}
                      </>
                    )}
                    {/* a charge the bank feed withdrew is not a bill that
                        never posted — its own line, as on the web */}
                    {((d.irregular_bills ?? []).length > 0
                      || lateBills.length > 0)
                      && goneBills.length > 0 && "\n"}
                    {goneBills.length > 0 && (
                      <Text style={{ color: C.warn }}>
                        withdrawn by the bank feed:{" "}
                        {goneBills.slice(0, 4).map(([p, a, , on], k) => (
                          <Text key={k}>
                            {p} {money(a, false)} ({mmdd(on)})
                            {k < Math.min(goneBills.length, 4) - 1
                              ? ", " : ""}
                          </Text>
                        ))}
                      </Text>
                    )}
                  </Text>
                ) : undefined}
              />
            </View>
            );
          })()}

          {/* detail-only, like the map */}
          {adv && (fc || d.recent.length > 0 || paidRows.length > 0) && (
            <View style={s.card}>
              <Text style={s.h2}>
                Cash timeline{" "}
                <Text style={[s.mut, { fontWeight: "400" }]}>
                  {fc ? `recent activity & next ${fc.days} days`
                      : `(${mmdd(yesterday)} – ${mmdd(d.date)} · variable `
                        + `spending ${money(recentSum, false)})`}
                </Text>
              </Text>
              {fc && (
                <View style={{ flexDirection: "row", flexWrap: "wrap",
                               gap: 14 }}>
                  <Kpi label="checking now" value={fc.checking} />
                  <Kpi label="card debt" value={fc.card_debt} plain />
                  {/* paychecks still due, then the balance the month
                      ends on — the same day the timeline's month-end row
                      shows (todayview.month_end_left), with the server's
                      note under it. An older server sends no month-end
                      figure, and the tile is left out. */}
                  {typeof d.income_due_total === "number" && (
                    <View>
                      <Text style={s.kpiLbl}>paychecks still due</Text>
                      <Text style={[s.kpi, { color: C.good }]}>
                        +{money(d.income_due_total, false)}</Text>
                      {(d.income_due_dates ?? []).length > 0 && (
                        <Text style={{ color: C.mut, fontSize: 10 }}>
                          {(d.income_due_dates ?? []).slice(0, 3).map(mmdd).join(", ")}
                        </Text>)}
                    </View>
                  )}
                  {typeof d.month_end_left === "number" && (
                    <View style={{ maxWidth: 220 }}>
                      {/* the server's label (older-server path: stands in for `month_end_label`) */}
                      <Kpi label={d.month_end_label !== undefined
                                    ? (d.month_end_label ?? "")
                                    : "left at month end"}
                           value={d.month_end_left} signed />
                      {/* its notes (what it counts, what the cards hold
                          then) are in "About this forecast" below: a phone
                          keeps the figures up top and the sentences a tap
                          away */}
                    </View>
                  )}
                  {/* the card scenarios' lows: the selected view's, under
                      the selector below (the web shows all three side by
                      side; a phone shows the one being looked at) */}
                  {scens ? null : converged ? (
                    <View>
                      <Text style={s.kpiLbl}>low point</Text>
                      <Text style={[s.kpi,
                                    p0!.min < 0 && { color: C.bad }]}>
                        {money(p0!.min, false)}{" "}
                        <Text style={s.mut}>{mmdd(p0!.min_date)}</Text>
                      </Text>
                      {(() => {
                        const negs = lows.filter((l) => l.s.negative_date);
                        if (!negs.length) return null;
                        return (
                          <Text style={{ color: C.mut, fontSize: 11 }}>
                            {negs.every((l) => l.s.negative_date
                                === negs[0].s.negative_date)
                              ? `negative from ${
                                  mmdd(negs[0].s.negative_date!)}`
                              : "negative from: " + negs.map((l) =>
                                  `${l.short} ${
                                    mmdd(l.s.negative_date!)}`).join(" · ")}
                          </Text>
                        );
                      })()}
                    </View>
                  ) : lows.map((l) => (
                    <View key={l.label}>
                      <Text style={s.kpiLbl}>low — {l.label}</Text>
                      <Text style={[s.kpi,
                                    l.s.min < 0 && { color: C.bad }]}>
                        {money(l.s.min, false)}{" "}
                        <Text style={s.mut}>{mmdd(l.s.min_date)}</Text>
                      </Text>
                      {l.s.negative_date && (
                        <Text style={{ color: C.mut, fontSize: 11 }}>
                          negative from {mmdd(l.s.negative_date)}
                        </Text>
                      )}
                    </View>
                  ))}
                </View>
              )}
              {/* one selector for the chart and the timeline below it */}
              {fc && scens && scens.length > 1 && (
                <SegmentedControl label="Card payments" size="sm"
                  value={scen?.key ?? null} onChange={setScenKey}
                  style={{ marginTop: 6 }}
                  options={scens.map((x) => ({ value: x.key, label: x.label }))} />
              )}
              {/* the selected view's low point, in the server's words, with
                  what that walk still holds on the cards that day */}
              {fc && scens && scens.filter((x) => x.key === scen?.key).map((x) => (
                <View key={x.key} style={{ marginTop: 6 }}>
                  <Text style={s.kpiLbl}>low — {x.label.toLowerCase()}</Text>
                  <Text style={[s.mut, { fontSize: 13 },
                                x.min < 0 && { color: C.bad }]}>
                    {x.low_words}</Text>
                  {x.low_cards_note ? (
                    <Text style={{ color: C.mut, fontSize: 10 }}>
                      {x.low_cards_note}
                    </Text>
                  ) : null}
                </View>
              ))}
              {fc && scen && (
                <Sparkline width={width - 24 - 28} height={120}
                  subject={`Cash timeline for the next ${fc.days} days, ${scen.label.toLowerCase()}`}
                  data={scen.series}
                  markX={(() => {
                    const i = scen.series.findIndex(([, v]) => v < 0);
                    return i === -1 ? null : i;
                  })()}
                  markLabel={scen.runs_out_label}
                  // each card's due dates in every scenario, the web's
                  // (older-server path: stands in for `due_marks`)
                  events={scen.due_marks !== undefined
                    ? dueMarks(scen.series, scen.due_marks ?? [])
                    : autopayMarks(scen.series, scen.marks)}
                  // the range the forecast's own past misses suggest,
                  // landed, shaded, when the server sends it
                  band={scen.band ?? undefined}
                  // older-server path: stands in for `band` — the same
                  // walk in a heavier month, dashed beside it
                  second={scen.band ? undefined : scen.heavy ?? undefined}
                  // the second line's name up to its dash ("heavier
                  // month"); the whole label is under "About this forecast"
                  legend={[scen.label, (scen.band
                                     // older-server path: stands in for `band_label`
                                     ? scen.band_label ?? ""
                                     : scen.heavy_label ?? "").split(" — ")[0]]} />
              )}
              {/* older-server path: stands in for `scenarios` */}
              {fc && !scens && (
                <Sparkline width={width - 24 - 28} height={120}
                  subject={`Cash timeline for the next ${fc.days} days, autopay statement balance`}
                  data={fc.pace_stmt.series}
                  second={fc.pace_now.series}
                  markX={(() => {
                    const i = fc.pace_stmt.series
                      .findIndex(([, v]) => v < 0);
                    return i === -1 ? null : i;
                  })()}
                  // the server's mark, in the page's MM/DD/YY (an older
                  // server sends none, and the chart draws no label)
                  markLabel={fc.runs_out_label ?? ""}
                  events={autopayMarks(fc.pace_stmt.series, fc.card_autopay)}
                  legend={["autopay statement balance",
                           "pay all cards now"]} />
              )}
              {/* how close the forecast has landed
                  (todayview.score_sentences), printed as sent; an older
                  server sends no `score` and the phone prints none
                  (older-server path: stands in for `score` — nothing) */}
              {fc?.score?.text && client ? (
                <ForecastRecord client={client} text={fc.score.text}
                                width={width - 24 - 28} />
              ) : null}
              {/* a paycheck the forecast leaves out because it stopped
                  arriving: said outright, not folded away — the walk is
                  missing income the person may expect (the web's line) */}
              {fc?.stopped_paychecks_note ? (
                <Text style={{ color: C.warn, fontSize: 11, marginTop: 4 }}>
                  {fc.stopped_paychecks_note}
                </Text>
              ) : null}
              {/* every sentence about how the forecast is made, one tap
                  away: a phone's screen is for the figures and the chart.
                  All of it is the server's words, the web's lines. */}
              {fc && (
                <Pressable {...disclosure(aboutOpen, "About this forecast")}
                           onPress={() => setAboutOpen(!aboutOpen)}
                           style={{ marginTop: 6 }}>
                  <Text style={{ color: C.mut, fontSize: 11 }}>
                    {aboutOpen ? "▾ " : "▸ "}About this forecast</Text>
                </Pressable>
              )}
              {fc && aboutOpen && (
                <View>
                  {d.month_end_note ? (
                    <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>
                      {/* the month-end tile's label, then its two notes */}
                      {d.month_end_label ?? "left at month end"}: {d.month_end_note}
                      {d.month_end_cards_note ? ` ${d.month_end_cards_note}` : ""}
                    </Text>
                  ) : null}
                  {scen && !scen.band && scen.heavy_label ? (
                    <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>
                      {scen.heavy_label}
                    </Text>
                  ) : null}
                  {/* the one-offs the daily pace leaves out — the server's
                      sentence, the web's and the email's */}
                  {fc?.pace_note ? (
                    <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>
                      {fc.pace_note}
                    </Text>
                  ) : null}
                  {/* the band's sentence while it is drawn — the server's */}
                  {scen?.band_text ? (
                    <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>
                      {scen.band_text}
                    </Text>
                  ) : null}
                  {fc?.score?.bias_text ? (
                    <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>
                      {fc.score.bias_text}
                    </Text>
                  ) : null}
                  {fc?.score?.note ? (
                    <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>
                      {fc.score.note}
                    </Text>
                  ) : null}
                  {/* card spending the selected walk still owes at its end */}
                  {scen?.cards_note ? (
                    <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>
                      {scen.cards_note}
                    </Text>
                  ) : null}
                  {/* when card spending reaches checking — the server's words */}
                  {fc?.card_timing_note ? (
                    <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>
                      {fc.card_timing_note}
                    </Text>
                  ) : null}
                  {/* own-account transfers the forecast leaves out — the
                      server's words (the web's line) */}
                  {fc?.transfers_note ? (
                    <Text style={{ color: C.mut, fontSize: 11, marginTop: 4 }}>
                      {fc.transfers_note}
                    </Text>
                  ) : null}
                </View>
              )}
              {/* column headers, like the web's cash-tl table at phone
                  width: the rows' after_cards (the web's hide-m column,
                  headed fc.after_cards_label) is not printed here */}
              {(fc || paidRows.length > 0 || shownFuture.length > 0) && (
                <View style={[s.tlRow, { marginTop: 8 }]}>
                  <Text style={[s.tlDate, { color: C.mut, fontSize: 10 }]}>
                    Date</Text>
                  <Text style={[s.tlLabel, { color: C.mut, fontSize: 10 }]}>
                    Event</Text>
                  <Text style={[s.tlAmt, { color: C.mut, fontSize: 10 }]}>
                    Amount</Text>
                  {fc && (
                    <Text style={[s.tlBal, { color: C.mut, fontSize: 10 }]}>
                      Balance</Text>
                  )}
                </View>
              )}
              {paidRows.map((r, i) => (
                <View key={`p${i}`} style={s.tlRow}>
                  <Text style={s.tlDate} numberOfLines={1}>{mmdd(r.date)}</Text>
                  <Text style={[s.tlLabel, { color: C.mut }]}
                        numberOfLines={1}>{r.label}</Text>
                  <Text style={s.tlAmt}>{money(r.amount, false)}</Text>
                </View>
              ))}
              {d.recent.map((t) => (
                <TxnRow key={t.id} t={t} onPress={openTxn} />))}
              {fc && (
                <View style={s.todayRow}>
                  <Text style={{ color: C.accent, fontSize: 12,
                                 fontWeight: "700" }}>
                    TODAY
                    <Text style={[s.mut, { fontWeight: "400" }]}>
                      {"  checking "}{money(fc.checking, false)} · recent
                      variable spending {money(recentSum, false)}
                    </Text>
                  </Text>
                </View>
              )}
              {shownFuture.map((r, i) => (
                <View key={`f${i}`} style={s.tlRow}>
                  <Text style={s.tlDate} numberOfLines={1}>
                    {nearDay(r.date, d.date) ?? mmdd(r.date)}</Text>
                  {/* a bill on a card moves no checking balance: greyed */}
                  <Text style={[s.tlLabel,
                                (r.bal_now === null || !!r.card) && { color: C.mut }]}
                        numberOfLines={1}>{r.label}</Text>
                  {/* a bill on a card greys its amount too, as the web's
                      does: it moves no checking balance that day */}
                  <Text style={[s.tlAmt, r.bal_now !== null && !r.card
                                  && r.amount > 0 && { color: C.good },
                                (r.bal_now === null || !!r.card)
                                  && { color: C.mut }]}>
                    {/* the month-end row is a balance with no amount */}
                    {r.kind === "month_end" ? "" : money(r.amount, false)}
                  </Text>
                  {/* without checking the walk's balances start from
                      nothing — the events stand, their balances do not */}
                  <Text style={[s.tlBal, !noChecking && r.bal_stmt !== null
                                  && r.bal_stmt < 0 && { color: C.bad }]}>
                    {!noChecking && r.bal_stmt !== null
                      ? money(r.bal_stmt, false) : "·"}
                  </Text>
                </View>
              ))}
              {futureRows.length > shownFuture.length && !allEvents && (
                <Pressable accessibilityRole="button" onPress={() => setAllEvents(true)}>
                  <Text style={{ color: C.accent, fontSize: 13,
                                 marginTop: 4 }}>
                    all {futureRows.length} events →
                  </Text>
                </Pressable>
              )}
            </View>
          )}

        </>
      )}
    </ScrollView>
  );
}

// bold-ink inline number, the web planflow's <b> — Text nesting keeps it
function B({ children }: { children: React.ReactNode }) {
  return (
    <Text style={{ color: C.text, fontWeight: "700" }}>{children}</Text>
  );
}

function Kpi({ label, value, plain, signed }:
    { label: string; value: number; plain?: boolean; signed?: boolean }) {
  const color = plain ? C.text
    : signed ? (value >= 0 ? C.good : C.bad)
    : value < 0 ? C.bad : C.text;
  return (
    <View>
      <Text style={s.kpiLbl}>{label}</Text>
      <Text style={[s.kpi, { color }]}>{money(value, false)}</Text>
    </View>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  content: { padding: 12, gap: 10 },
  card: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 12, padding: 14, gap: 6 },
  row: { flexDirection: "row", justifyContent: "space-between",
         alignItems: "center" },
  verdict: { fontSize: 30, fontWeight: "800" },
  starLbl: { color: C.mut, fontSize: 12, textTransform: "uppercase",
             letterSpacing: 1, marginTop: 10 },
  tiles: { flexDirection: "row", flexWrap: "wrap", gap: 12, marginTop: 4 },
  lblPress: { alignSelf: "flex-start", marginLeft: -4, paddingHorizontal: 4, borderRadius: 6 },
  tile: { flexBasis: "45%", flexGrow: 1, gap: 2 },
  tileBig: { color: C.text, fontSize: 26, fontWeight: "800",
             fontVariant: ["tabular-nums"] },
  tileSub: { color: C.mut, fontSize: 11 },
  h2: { color: C.text, fontSize: 16, fontWeight: "600" },
  mut: { color: C.mut, fontSize: 13 },
  err: { color: C.bad, fontSize: 14 },
  watch: { fontSize: 13, lineHeight: 19, marginTop: 8 },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 8, paddingVertical: 3,
          maxWidth: 220 },
  kpiLbl: { color: C.mut, fontSize: 11 },
  kpi: { color: C.text, fontSize: 17, fontWeight: "700",
         fontVariant: ["tabular-nums"] },
  tlRow: { flexDirection: "row", alignItems: "center", gap: 8,
           borderTopColor: C.border,
           borderTopWidth: StyleSheet.hairlineWidth,
           paddingVertical: 5 },
  // wide enough for a dated-with-year 09/08/26 on one line — the column
  // was sized for 09/08 and wrapped the year onto a second line
  tlDate: { color: C.mut, fontSize: 12, width: 64 },
  tlLabel: { color: C.text, fontSize: 13, flex: 1 },
  tlAmt: { color: C.text, fontSize: 13, fontVariant: ["tabular-nums"] },
  tlBal: { color: C.mut, fontSize: 13, fontVariant: ["tabular-nums"],
           width: 64, textAlign: "right" },
  todayRow: { borderBottomColor: C.accent, borderBottomWidth: 1,
              paddingVertical: 5, marginTop: 2 },
  btn: { backgroundColor: C.btn, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8, marginTop: 8,
         alignSelf: "flex-start" },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" as const },
  // message, action, dismiss; the left edge carries how serious it is
  // the web's .needsrow: one slim line, severity on the left edge
  needsRow: { flexDirection: "row", alignItems: "center", gap: 8,
              minHeight: 44,
              paddingHorizontal: 12, paddingVertical: 6,
              backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
              borderLeftWidth: 4, borderRadius: 10 },
  alert: { flexDirection: "row", alignItems: "center", gap: 8,
           backgroundColor: C.card, borderWidth: 1, borderColor: C.border,
           borderLeftWidth: 4, borderRadius: 10,
           paddingLeft: 12, paddingVertical: 6 },
});
