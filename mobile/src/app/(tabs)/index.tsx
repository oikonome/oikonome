// Today — the verdict, what's left to spend today, and the recent pane.
// Read-only mirror of the web Today page's core; every number is
// server-composed.
import AsyncStorage from "@react-native-async-storage/async-storage";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter, useScrollToTop } from "expo-router";
import { useEffect, useRef, useState } from "react";
import { Alert as RNAlert, Pressable, ScrollView, StyleSheet, Text, View,
         useWindowDimensions } from "react-native";
import PullRefresh from "../../components/pull-refresh";

import MoneyMap, { planRows, ledgerRoute } from "../../components/money-map";
import Sparkline from "../../components/sparkline";
import StaleBanner from "../../components/stale-banner";
import TxnRow from "../../components/txn-row";
import { Meter } from "../../components/ui";
import { alertRoute } from "../../lib/alert-routes";
import { consumeLaunch, launchSettled } from "../../lib/launch";
import { errText, setHandedOffTxn, type Alert, type AlertRow,
         type TodayFull } from "../../lib/api";
import { patchList, patchQuery, removeFromList,
         saveSettings } from "../../lib/cache";
import { shiftYmd, ymd } from "../../lib/dates";
import { autopayMarks, resolveFace } from "../../lib/pure";
import { useSession } from "../../lib/session";
import { hasLeftWizard } from "../../lib/storage";
import { heroLabel, VERDICT } from "../../lib/verdict";
import { useDemo, useViewer } from "../../lib/viewer";
import { C, mmddyy, money } from "../../lib/theme";

// where a demo instance keeps the hero face, since it cannot keep it on
// the account (mirrors the web's oiko-today-face)
const FACE_KEY = "oikonome.todayFace";


export default function Today() {
  const { client, serverUrl } = useSession();
  // the household's default tab, applied ONCE per app launch: Today is
  // the navigator's home, so the first mount of this screen is the moment
  // the app "opens" — a later tap on the Today tab really means Today
  const router0 = useRouter();
  const homeQ = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
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
  // the home-screen widget shows this hero's simple face; whenever the
  // live Today payload settles, refresh the widget from the same door so
  // opening the app never leaves the launcher showing an older verdict
  useEffect(() => {
    if (!client || viewDate || !q.data || q.isFetching) return;
    import("../../lib/glance").then(({ refreshWidget }) =>
      refreshWidget(client)).catch(() => {});
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
  // Dismissing is certain, so the banner leaves in the same frame as the
  // tap (rolled back if the server refuses); the alert history learns
  // the flag from the response and is the only thing refetched — none of
  // Today's numbers move when an alert is waved away.
  const dismissAlert = useMutation({
    mutationFn: ({ kind, message }: { kind: string; message: string }) =>
      client!.alertDismiss(kind, message),
    onMutate: async ({ kind, message }) => {
      await qc.cancelQueries({ queryKey: ["today"], exact: true });
      const alerts = qc.getQueryData<TodayFull>(["today"])?.alerts ?? [];
      const at = alerts.findIndex(
        (a) => a.kind === kind && a.message === message);
      removeFromList<TodayFull, Alert>(qc, ["today"], "alerts",
        (a) => a.kind === kind && a.message === message);
      return { gone: at >= 0 ? alerts[at] : undefined, at };
    },
    onError: (e, _v, ctx) => {
      // a dismiss that silently fails leaves a ghost the user thinks is
      // gone — put THAT alert back and say so (the web toasts it). Only
      // that one: restoring the whole snapshot would resurrect an alert
      // a second, successful dismiss took out meanwhile.
      const gone = ctx?.gone;
      if (gone)
        qc.setQueryData<TodayFull>(["today"], (old) => {
          if (!old || old.alerts.some(
                (a) => a.kind === gone.kind && a.message === gone.message))
            return old;
          const alerts = [...old.alerts];
          alerts.splice(Math.min(ctx.at, alerts.length), 0, gone);
          return { ...old, alerts };
        });
      RNAlert.alert("Dismiss failed", errText(e));
    },
    onSuccess: (_r, { kind, message }) => {
      patchList<{ alerts: AlertRow[] }, AlertRow>(qc, ["alerts"], "alerts",
        (a) => a.kind === kind && a.message === message, { dismissed: 1 });
      qc.invalidateQueries({ queryKey: ["alerts"] });
    },
  });
  const [whyOpen, setWhyOpen] = useState(false);
  // top-pane meters are DAY-scoped, the same rule as the web: today's
  // spend against today's allowance — the month gauges live in the
  // Monthly budget card. Totalled across buckets for the simple face.
  const dayTiles = d ? [d.allow?.food, d.allow?.other,
    ...(d.allow_kids ?? [])].filter(Boolean) : [];
  // Mirrors the server's simple_day_meter (and the web): spend counts
  // against the bar only up to each bucket's own allowance, and the true
  // overshoot returns once every bucket is exhausted — so the bar always
  // agrees with the headline number and the email.
  // left_today is the server's own rounded per-bucket remainder — summing
  // it (not re-deriving allowance − spent) keeps this bar equal to the
  // headline to the cent, like the web.
  const dayAllow = dayTiles.reduce((s2, a) => s2 + a!.today_allowance, 0);
  const dayLeft = dayTiles.reduce((s2, a) =>
    s2 + Math.max(0, a!.left_today), 0);
  const daySpent = dayAllow - dayLeft + (dayLeft < 0.005
    ? dayTiles.reduce((s2, a) => s2 + Math.max(0, -a!.left_today), 0)
    : 0);
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
  // setup incomplete → the checklist card. OWNER only — every step is
  // a 403 for a viewer (web gates the same way)
  const setupOpen = !viewer && ob.data && (!ob.data.budgets_set
    || ob.data.transactions === 0 || ob.data.accounts === 0
    || ob.data.bills === 0);

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
                        enabled: !!client });
  const tenantId = me.data?.tenant_id;
  const [leftWiz, setLeftWiz] = useState<boolean | null>(null);
  useEffect(() => {
    if (!tenantId) return;
    let live = true;
    hasLeftWizard(tenantId).then((v) => { if (live) setLeftWiz(v); });
    return () => { live = false; };
  }, [tenantId]);
  const wantsWizard = !!setupOpen && !ob.data?.wizard_done && leftWiz === false;
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
  const fc = d?.forecast ?? null;
  const p0 = fc?.pace_stmt ?? null;
  const negIn = p0?.negative_date && d
    ? Math.round((new Date(p0.negative_date).getTime()
                  - new Date(d.date).getTime()) / 86400000)
    : null;
  const paycheckish = d?.income ? d.income / 2 : 1000;
  const cashCritical = !!d
    && ((d.headroom !== null && d.headroom < 0)
        || (negIn !== null && negIn <= 14));
  const cashTight = !!d && !cashCritical
    && ((d.headroom !== null && d.headroom < paycheckish)
        || negIn !== null || !!(p0 && p0.min < 0));
  // the shortfall beside negative_date is the balance ON that day —
  // min can be a deeper trough weeks later
  const firstNeg = p0?.negative_date
    ? (p0.first_neg_amount ?? p0.min) : null;
  const slowTo = firstNeg !== null && firstNeg < 0 && negIn && negIn > 0
    ? Math.max(0, (p0!.rate ?? 0) + firstNeg / negIn) : null;

  // ---- cash timeline: paid ✓ rows arrive in forecast_rows with no
  // balance; future events show through the low point by default ----
  const paidRows = (d?.forecast_rows ?? []).filter(
    (r) => r.bal_now === null && r.bal_stmt === null && r.date <= d!.date);
  const futureRows = (d?.forecast_rows ?? [])
    .filter((r) => !paidRows.includes(r));
  const preLow = p0?.min_date
    ? futureRows.filter((r) => r.date <= p0.min_date) : futureRows;
  const shownFuture = allEvents || preLow.length === 0
    ? futureRows : preLow;
  const recentSum = (d?.recent ?? []).reduce((s0, r) => s0 + r.amount, 0);
  // dates carry the year, like the web's mmdd() — a truncated 08/10
  // reads wrong the moment a forecast or ETA crosses New Year
  const mmdd = mmddyy;
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
      <StaleBanner query={q} />
      {!d && !q.isError && <Text style={s.mut}>Loading…</Text>}
      {q.isError && !d && (
        <Text style={s.err}>Couldn't reach the server.</Text>
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
          {d.is_today && setupOpen && !ob.data!.wizard_done && (
            <View style={s.card}>
              <Text style={s.h2}>Welcome — let&apos;s finish setting up</Text>
              <Text style={s.mut}>
                The guided setup picks up right where you left off —
                connect accounts, bring in history, confirm bills, set
                budgets.
              </Text>
              <Pressable style={s.btn}
                         onPress={() => router.push("/welcome" as never)}>
                <Text style={s.btnText}>Resume setup →</Text>
              </Pressable>
            </View>
          )}
          {d.is_today && setupOpen && ob.data!.wizard_done && (
            <View style={s.card}>
              <Text style={s.h2}>Welcome — let&apos;s finish setting up</Text>
              {ob.data && (() => {
                const steps = [ob.data.accounts > 0,
                               ob.data.transactions > 0,
                               ob.data.bills > 0, ob.data.budgets_set,
                               ob.data.primary_checking_set];
                const done = steps.filter(Boolean).length;
                return (
                  <Text style={s.mut}>
                    {done}/{steps.length} done. Every step is reversible.
                  </Text>
                );
              })()}
              {([
                [ob.data!.accounts > 0,
                 `Connect or add accounts (${ob.data!.accounts})`,
                 "/accounts"],
                [ob.data!.transactions > 0,
                 `Bring in transactions (${ob.data!.transactions})`,
                 "/imports"],
                [ob.data!.bills > 0,
                 `Confirm recurring bills (${ob.data!.bills})`,
                 "/bills"],
                [ob.data!.budgets_set, "Set your budgets", "/budget"],
                [ob.data!.primary_checking_set,
                 "Pick your primary checking", "/accounts"],
              ] as [boolean, string, string][]).map(([done, label,
                                                     href]) => (
                <Text key={label}
                      style={{ color: done ? C.mut : C.accent,
                               fontSize: 13, paddingVertical: 2,
                               textDecorationLine: done
                                 ? "line-through" : "none" }}
                      onPress={done ? undefined
                        : () => router.push(href as never)}>
                  {done ? "☑" : "☐"} {label}
                </Text>
              ))}
              {ob.data!.pending_proposals > 0 && (
                <Text style={{ color: C.warn, fontSize: 12 }}>
                  {ob.data!.pending_proposals} detected bill
                  {ob.data!.pending_proposals === 1 ? "" : "s"} await
                  review on Bills.
                </Text>
              )}
            </View>
          )}
          {(d.alerts ?? []).map((a, i) => {
            const tone = a.severity === "bad" ? C.bad
              : a.severity === "good" ? C.good
              : a.severity === "info" ? C.accent : C.warn;
            const glyph = a.severity === "good" ? "✓"
              : a.severity === "info" ? "•" : "⚠";
            const dismiss = () =>
              dismissAlert.mutate({ kind: a.kind, message: a.message });
            const route = alertRoute(a);
            return (
              <View key={`${a.kind}-${i}`}
                    style={[s.alert, { borderColor: tone }]}>
                <Text style={{ color: tone, fontSize: 13, flex: 1,
                               lineHeight: 18 }}
                      onPress={route ? () => {
                        // following the fix link counts as acting on it —
                        // it lands in alert history, restorable (web rule)
                        if (!viewer) dismiss();
                        router.push(route as never);
                      } : undefined}>
                  {glyph}{" "}
                  <Text style={route
                          ? { textDecorationLine: "underline" } : undefined}>
                    {a.message}
                  </Text>
                </Text>
                {!viewer && (
                <Text style={{ color: C.mut, fontSize: 16, padding: 4 }}
                      onPress={dismiss}>
                  ✕
                </Text>
                )}
              </View>
            );
          })}
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
                         marginHorizontal: 12, marginTop: 10 }}>
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
                    {f}
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
                        onPress={atFloor ? undefined : () => shiftDay(-1)}>
                    ‹
                  </Text>
                );
              })()}
              {!d.is_today && (
                <Text style={{ color: C.accent, fontSize: 20,
                               paddingHorizontal: 8 }}
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
            <Text style={s.mut}>
              {/* whole dollars, like the web hero and the daily email */}
              <Text style={d.variable_budget - d.variable_actual < 0
                             ? { color: C.bad } : undefined}>
                {money(Math.abs(d.variable_budget - d.variable_actual),
                       false)}
              </Text>
              {d.variable_budget - d.variable_actual < 0
                ? ` over the ${money(d.variable_budget, false)}`
                : ` of ${money(d.variable_budget, false)} left`}
              {" · "}{d.days_left} day{d.days_left === 1 ? "" : "s"}
            </Text>

            <Text style={s.starLbl}>Left to spend today</Text>
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
              ].filter(([, a]) => a).map(([name, a]) => {
                const overT = a.left_today < -0.5;
                return (
                  <View key={name} style={{ marginTop: 10 }}>
                    {/* number stacked UNDER the label, left — the pane's
                        one anatomy, matching the simple face; its 26px
                        prominence kept */}
                    {/* the label opens the rows behind the number — the
                        category's ledger for the month on view */}
                    <Pressable onPress={() => router.push(ledgerRoute(tileTo(name)) as never)}
                               style={s.lblPress}>
                      {({ pressed }) => (
                        <Text style={[s.h2, pressed && { color: C.accent }]}>{name}</Text>)}
                    </Pressable>
                    <Text style={[s.tileBig,
                                  overT && { color: C.bad }]}>
                      {money(Math.max(0, a.left_today), false)}
                    </Text>
                    <Text style={s.tileSub}>
                      {overT
                        ? `over today by ${money(-a.left_today, false)
                            } · spent ${money(a.spent_today, false)} of ${
                            money(a.today_allowance, false)}`
                        : a.spent_today > 0.005
                          ? `of ${money(a.today_allowance, false)
                              } today · spent ${money(a.spent_today, false)}`
                          : `of ${money(a.today_allowance, false)
                              } today · nothing spent yet`}
                    </Text>
                    <Meter big frac={dayFrac(a.spent_today, a.today_allowance)}
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
                <Text style={s.starLbl}>Pinned bills</Text>
                {d.bill_cards.map((b) => (
                  <View key={b.label} style={{ gap: 2, marginTop: 6 }}>
                    {/* number under the label, left — the pane's one
                        anatomy */}
                    {/* the label opens the bill's history */}
                    <Pressable onPress={() => router.push({ pathname: "/bill-history",
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
            ) : cashCritical ? (
              <Text style={[s.watch, { color: C.bad }]}>
                {d.headroom !== null && d.headroom < 0
                  ? `● Short ${money(-d.headroom, false)} today — checking `
                    + "doesn't cover cards & bills before the next "
                    + "paycheck. Cover it from savings"
                  : `● Short ${money(-(firstNeg ?? p0!.min), false)} by ${
                      p0!.negative_date ? mmdd(p0!.negative_date) : "now"
                    } — cover it from savings`}
                {slowTo !== null
                  ? `, or slow spending to ${money(slowTo, false)}/day.`
                  : "."}
              </Text>
            ) : cashTight ? (
              <Text style={[s.watch, { color: C.warn }]}>
                {p0 && (p0.min < 0 || negIn !== null)
                  ? `● Cash gets tight around ${mmdd(p0.min_date)} (low ${
                      money(p0.min, false)}).`
                  : `● Cash is tight — ${money(d.headroom ?? 0, false)
                      } left after cards & bills before the next paycheck.`}
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

            {adv && d.plan_surplus !== null && d.buckets && (
              <Text style={[s.mut, { marginTop: 8, borderTopColor: C.border,
                                     borderTopWidth: StyleSheet.hairlineWidth,
                                     paddingTop: 8 }]}>
                Plan: <B>{money(d.income ?? 0, false)}</B> in →{" "}
                <B>{money(d.buckets.fixed?.month_budget ?? 0, false)}</B> bills →{" "}
                <B>{money(d.variable_budget, false)}</B> to spend →{" "}
                {(d.savings_plan ?? 0) > 0.005 ? (
                  <>
                    <B>{money(d.savings_plan!, false)}</B> saved →{" "}
                  </>
                ) : null}
                <Text style={{ color: d.plan_surplus >= 0 ? C.good : C.bad,
                               fontWeight: "700" }}>
                  {money(d.plan_surplus, false)}
                </Text> excess
                {/* sweep goals sit beside the chain, not inside it — the
                    sentence is server-composed so no surface can drift */}
                {d.sweep_line ? <> · {d.sweep_line}</> : null}
                {"   "}
                <Text style={{ color: C.accent, fontWeight: "400" }}
                      onPress={() => router.push("/budget" as never)}>
                  balance the budget →
                </Text>
              </Text>
            )}
          </View>
          </>)}

          {/* only when a TRACKABLE goal exists — a card of plan-only
              zero rows is chrome, not information (the web's filter) */}
          {adv && (d.savings_goals ?? []).some((g) => !g.plan_only) && (
            <View style={s.card}>
              <Text style={s.h2}>Savings goals</Text>
              {d.savings_goals.filter((g) =>
                  !(g.plan_only && !g.monthly_plan && !g.target)).map((g) => {
                // the web's pace phrase, verbatim priority order — and the
                // web's gate: plan_only alone (a null saved with a real
                // destination is "no contributions", not "plan only")
                const pace = g.plan_only
                  ? "plan only — set a destination account on the "
                    + "Budget page to track progress"
                  : g.saved === null
                    ? "no contributions in 90 days"
                  : !g.target
                    ? ((g.rate_90d ?? 0) > 0
                        ? `saving ${money(g.rate_90d!, false)}/mo`
                        : "no contributions in 90 days")
                  : g.pct != null && g.pct >= 100 ? "funded ✓"
                  : g.on_pace === false
                    ? `off pace for ${mmdd(g.target_date!)}`
                  : g.on_pace ? `on pace for ${mmdd(g.target_date!)}`
                  : g.eta ? `~${mmdd(g.eta)} at the current rate`
                  : "no contributions in 90 days";
                const paceColor = g.on_pace === false ? C.bad
                  : g.plan_only || g.saved === null ? C.mut
                  : g.pct != null && g.pct >= 100 ? C.good : C.mut;
                return (
                <View key={g.name} style={{ gap: 2, marginTop: 4 }}>
                  <View style={s.row}>
                    <Text style={{ color: C.text, fontSize: 14,
                                   fontWeight: "600" }}>{g.name}</Text>
                    <Text style={{ color: C.mut, fontSize: 13 }}>
                      {g.plan_only || g.saved === null
                        ? `plan ${money(g.monthly_plan, false)}/mo`
                        : g.target
                          ? `${money(g.saved, false)} of ${
                              money(g.target, false)}${
                              g.pct != null ? ` (${g.pct}%)` : ""}`
                          : `${money(g.saved, false)} saved`}
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
          {adv && d.buckets && (
            <View style={s.card}>
              <Text style={s.h2}>Monthly budget</Text>
              <MoneyMap
                rows={planRows(d.buckets, { period,
                  fixedActual: d.buckets.fixed?.actual ?? 0,
                  fixedBudget: d.buckets.fixed?.month_budget ?? 0,
                  fixedPending: d.fixed_unpaid_due,
                  savings: (() => {
                    const g = (d.savings_goals ?? [])
                      .find((x) => x.name === "Savings");
                    return g ? { plan: g.monthly_plan, saved: g.rate_90d }
                             : null;
                  })(),
                  excess: d.plan_surplus,
                })}
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
                      && (d.overdue_unpaid ?? []).length > 0 && "\n"}
                    {(d.overdue_unpaid ?? []).length > 0 && (
                      <>
                        Scheduled but not yet posted:{" "}
                        {d.overdue_unpaid!.slice(0, 4).map(([p, a, due], k) => (
                          <Text key={k}>
                            {p} {money(a, false)} (due {mmdd(due)})
                            {k < Math.min(d.overdue_unpaid!.length, 4) - 1
                              ? ", " : ""}
                          </Text>
                        ))}
                      </>
                    )}
                  </Text>
                ) : undefined}
              />
            </View>
          )}

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
                  {d.runway
                    && typeof d.bills_remaining_month === "number" && (
                    <View>
                      <Kpi label="after cards & bills"
                           value={d.runway.checking - d.runway.card_debt
                                  - d.bills_remaining_month} signed />
                      {/* the web meter's tooltip, said in place */}
                      <Text style={{ color: C.mut, fontSize: 10 }}>
                        checking − card debt − bills still due
                      </Text>
                    </View>
                  )}
                  {/* the web's converged rule + labels: one line only
                      when the scenarios share min AND min_date */}
                  {converged ? (
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
              {fc && (
                <Sparkline width={width - 24 - 28} height={120}
                  data={fc.pace_stmt.series}
                  second={fc.pace_now.series}
                  markX={(() => {
                    const i = fc.pace_stmt.series
                      .findIndex(([, v]) => v < 0);
                    return i === -1 ? null : i;
                  })()}
                  markLabel={fc.pace_stmt.negative_date
                    ? "runs out " + mmdd(fc.pace_stmt.negative_date)
                        .replace(/^0/, "").replace(/\/0/, "/")
                    : ""}
                  events={autopayMarks(fc.pace_stmt.series, fc.card_autopay)}
                  legend={["autopay statement balance",
                           "pay all cards now"]} />
              )}
              {/* column headers, like the web's cash-tl table */}
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
                  <Text style={s.tlDate}>{mmdd(r.date)}</Text>
                  <Text style={[s.tlLabel, { color: C.mut }]}
                        numberOfLines={1}>{r.label}</Text>
                  <Text style={s.tlAmt}>{money(r.amount, false)}</Text>
                </View>
              ))}
              {d.recent.map((t) => (
                <TxnRow key={t.id} t={t} onPress={(x) => {
                  setHandedOffTxn(x);
                  router.push({ pathname: "/txn",
                                params: { id: x.id } }); }} />))}
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
                  <Text style={s.tlDate}>{mmdd(r.date)}</Text>
                  <Text style={[s.tlLabel,
                                r.bal_now === null && { color: C.mut }]}
                        numberOfLines={1}>{r.label}</Text>
                  <Text style={[s.tlAmt, r.bal_now !== null && r.amount > 0
                                  && { color: C.good }]}>
                    {money(r.amount, false)}
                  </Text>
                  <Text style={[s.tlBal, r.bal_stmt !== null
                                  && r.bal_stmt < 0 && { color: C.bad }]}>
                    {r.bal_stmt !== null ? money(r.bal_stmt, false) : "·"}
                  </Text>
                </View>
              ))}
              {futureRows.length > shownFuture.length && !allEvents && (
                <Pressable onPress={() => setAllEvents(true)}>
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
  tlDate: { color: C.mut, fontSize: 12, width: 38 },
  tlLabel: { color: C.text, fontSize: 13, flex: 1 },
  tlAmt: { color: C.text, fontSize: 13, fontVariant: ["tabular-nums"] },
  tlBal: { color: C.mut, fontSize: 13, fontVariant: ["tabular-nums"],
           width: 64, textAlign: "right" },
  todayRow: { borderBottomColor: C.accent, borderBottomWidth: 1,
              paddingVertical: 5, marginTop: 2 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8, marginTop: 8,
         alignSelf: "flex-start" },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" as const },
  alert: { flexDirection: "row", alignItems: "center", gap: 6,
           backgroundColor: C.card, borderWidth: 1, borderRadius: 10,
           paddingHorizontal: 12, paddingVertical: 8 },
});
