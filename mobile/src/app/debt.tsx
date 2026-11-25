// Debt — every card and loan on one payoff schedule: the debt-free date
// hero with its three tiles, the method (avalanche / snowball) and
// extra-payment what-ifs, the balance curve under both methods, and the
// debts in payoff order with their rate and minimum editable in place.
// Mirrors webapp/src/pages/Debt.tsx — keep them in step.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Switch, Text, TextInput, View,
         useWindowDimensions } from "react-native";
import PullRefresh from "../components/pull-refresh";

import Sparkline from "../components/sparkline";
import { FailedLine, NoAccountsLine, StaleLine, useNoAccounts }
  from "../components/loaded";
import { Card, H, HelpLink, KV, Pill, Skeleton, Note } from "../components/ui";
import { errText, whatIfRefusal, type DebtMethod, type DebtPlan, type DebtPlanSave,
         type DebtRow } from "../lib/api";
import { moneyParam, monthYear, parseMoneyDraft, rebaseFields, rebaseKeyed,
         failLine, span } from "../lib/pure";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C, money } from "../lib/theme";
import SegmentedControl from "../components/segmented-control";
import { fieldA11y } from "../components/field";

const m$ = (n: number) => money(n, false);
// whether a schedule ends inside the fifty-year walk; one that does not
// carries fifty years of compounding as its interest, which nobody pays
// (web: pages/Debt.tsx finishes / noEnd)
const finishes = (x: { finishes?: boolean; debt_free: string | null }) =>
  x.finishes ?? x.debt_free !== null;
const noEnd = (x: { growing: boolean }) =>
  x.growing ? "grows — the minimums don't cover the interest"
    : "not paid off within fifty years";
const NAMES: Record<DebtMethod, string> =
  { avalanche: "Avalanche", snowball: "Snowball" };
const OTHER: Record<DebtMethod, DebtMethod> =
  { avalanche: "snowball", snowball: "avalanche" };
// where the walked minimum came from (web: MIN_FROM in pages/Debt.tsx); an
// older server sends only min_source
const MIN_FROM: Record<NonNullable<DebtRow["min_basis"]>, string> = {
  you: "yours", issuer: "issuer", estimate: "estimate",
  ledger: "from your payments",
};

type Draft = Record<string, { apr: string; min_payment: string; skip: boolean }>;
type PlanFields = { method: DebtMethod; extra_monthly: number | string };

const draftOf = (rows: DebtRow[]): Draft =>
  Object.fromEntries(rows.map((d) => [d.id, {
    // the box shows what the household typed; an issuer/estimated value
    // sits in the placeholder so clearing the box means "back to that"
    apr: d.apr_source === "you" ? String(d.apr) : "",
    min_payment: d.min_source === "you" ? String(d.min_payment) : "",
    skip: d.skip,
  }]));

// The planner reply also carries every stored override (the shared
// type predates it, so it is optional here).
type WithOverrides = DebtPlan & { overrides?: DebtPlanSave["debts"] };

// Stored overrides whose debt is not in the map being saved.
const keptOverrides = (stored: DebtPlanSave["debts"] | undefined,
                       saving: Record<string, unknown>): DebtPlanSave["debts"] =>
  Object.fromEntries(Object.entries(stored ?? {})
    .filter(([id]) => !Object.prototype.hasOwnProperty.call(saving, id)));

export default function Debt() {
  const { client } = useSession();
  const qc = useQueryClient();
  const { width } = useWindowDimensions();
  const viewer = useViewer();
  const [params, setParams] = useState<Record<string, string>>({});
  const [extra, setExtra] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  // the overrides as they were when the first box was edited
  const [opened, setOpened] = useState<Draft | null>(null);
  const noAccounts = useNoAccounts();
  const [err, setErr] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["debt-plan", params],
    queryFn: () => client!.debtPlan(params),
    enabled: !!client,
    placeholderData: (prev) => prev,
  });
  const save = useMutation({
    // The server replaces the whole plan, and the draft is a copy from
    // when this screen loaded: another device may have saved since. Read
    // the stored plan now and lay only this screen's changes over it.
    mutationFn: async (v: { plan: PlanFields; planBase: PlanFields;
                            mine: Draft; base: Draft }) => {
      const fresh: WithOverrides = await client!.debtPlan({});
      const plan = rebaseFields(v.plan, v.planBase, fresh.saved);
      const debts = rebaseKeyed(v.mine, v.base, draftOf(fresh.debts));
      const body: DebtPlanSave = {
        method: plan.method, extra_monthly: plan.extra_monthly,
        // the list holds only debts carrying a balance; a stored override
        // of one off it (paid to zero, excluded) rides along untouched, or
        // this save would erase it
        debts: { ...keptOverrides(fresh.overrides, debts), ...Object.fromEntries(Object.entries(debts).map(([id, x]) => [id, {
          apr: x.apr === "" ? null : x.apr,
          min_payment: x.min_payment === "" ? null : x.min_payment,
          skip: x.skip,
        }])) },
      };
      return client!.debtPlanSave(body);
    },
    onSuccess: (d) => {
      // the reply IS the saved plan: seed the unparameterised key and
      // drop the what-if so the screen reads what it just wrote
      qc.setQueryData(["debt-plan", {}], d);
      qc.invalidateQueries({ queryKey: ["debt-plan"] });
      setParams({}); setExtra(null); setDraft(null); setOpened(null);
      setErr(null);
    },
    onError: (e) => setErr(errText(e)),
  });
  // A failed what-if leaves query.data empty (placeholderData only covers
  // the pending fetch), and every control lives under `r`. Keep the last
  // plan the server did send, so a refused amount can be corrected in
  // place instead of taking the whole screen with it.
  const [lastGood, setLastGood] = useState<DebtPlan | undefined>();
  if (query.data && query.data !== lastGood) setLastGood(query.data);
  const r: DebtPlan | undefined = query.data ?? lastGood;
  const whatIfErr = query.isError && !query.data
    ? whatIfRefusal(query.error)
      ?? failLine(query.error, "the debt plan", "phone").text
    : null;
  const method = r?.method ?? "avalanche";
  const extraStr = extra ?? String(r?.extra_monthly ?? 0);
  const d = draft ?? (r ? draftOf(r.debts) : {});
  const unsaved = !!r && (method !== r.saved.method
    || r.extra_monthly !== r.saved.extra_monthly || draft !== null);
  // "1,000" from the phone keyboard is the amount the save door would
  // store; the what-if GET takes a plain number, so parse it here
  const extraQ = moneyParam(extraStr);
  const pick = (m: DebtMethod) => setParams({ method: m, extra: extraQ });
  const setRow = (id: string, patch: Partial<Draft[string]>) => {
    if (draft === null) setOpened(d);
    setDraft({ ...d, [id]: { ...d[id], ...patch } });
  };
  const persist = () => r && save.mutate({
    // the typed amount as a plain number, so "1,000" compares equal to the
    // stored 1000; a blank box is no extra (as on the web), and text that
    // is not an amount goes out as typed so the server's refusal names it
    // — never as 0, never as a null that would clear the saved extra
    plan: { method,
            extra_monthly: extraStr.replace(/[$\s]/g, "") === "" ? 0
              : parseMoneyDraft(extraStr) ?? extraStr },
    planBase: { method: r.saved.method, extra_monthly: r.saved.extra_monthly },
    mine: d, base: opened ?? d,
  });

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleLine query={query} />
      {query.isPending && !r && <Skeleton />}
      {query.isError && !r && (
        <FailedLine q={query} what="the debt plan" />
      )}
      {/* with nothing linked "Nothing owed" reads as a true zero */}
      {r && r.debts.length === 0 && noAccounts && <Card><NoAccountsLine /></Card>}
      {r && r.debts.length === 0 && !noAccounts && (
        <Card>
          <Text style={[s.big, { color: C.good }]}>
            {(r.paid_in_full ?? []).length ? "Nothing to pay off" : "Nothing owed"}
          </Text>
          <Text style={s.mut}>
            {(r.paid_in_full ?? []).length
              ? "Every card is paid in full each month, so none of it costs interest."
              : "No card or loan carries a balance."} A debt on an account the
            Accounts page excludes stays out of the plan too.
          </Text>
        </Card>
      )}
      {r && r.debts.length > 0 && (() => {
        const plan = r.plan, alt = r.alt, base = r.minimums;
        const live = r.debts.filter((x) => !x.skip);
        // the server's figures (web: pages/Debt.tsx): what the plan starts
        // from, and which debts it walks at a typical rate or an
        // estimated minimum
        const owed = plan.starting;
        const guessed = live.filter((x) => x.estimated === true).length;
        const savedVsMin = r.saves_vs_minimums ?? null;
        const altMore = r.alt_extra_interest ?? null;
        const byId = Object.fromEntries(plan.debts.map((x) => [x.id, x]));
        const ordered = [...r.debts].sort((a, b) =>
          (byId[a.id]?.order ?? 999) - (byId[b.id]?.order ?? 999));
        return (
          <>
            <Card>
              <View style={{ flexDirection: "row", alignItems: "center",
                             gap: 8, flexWrap: "wrap" }}>
                <Text style={s.mut}>
                  Debt-free — {NAMES[method].toLowerCase()}
                  {r.extra_monthly > 0
                    ? `, ${m$(r.extra_monthly)}/mo extra`
                    : ", no extra — each paid-off debt's minimum moves to the next"}
                </Text>
                {unsaved ? <Pill text="unsaved what-if" tone="warn" /> : null}
              </View>
              {plan.debt_free ? (
                <Text style={s.big}>
                  {monthYear(plan.debt_free)}
                  <Text style={{ color: C.good, fontSize: 13,
                                 fontWeight: "400" }}>
                    {"  "}in {span(plan.months)} — {m$(owed)} across{" "}
                    {live.length} {live.length === 1 ? "debt" : "debts"}
                  </Text>
                </Text>
              ) : (
                <Text style={[s.big, { color: C.bad }]}>
                  not within 50 years
                  <Text style={{ fontSize: 13, fontWeight: "400" }}>
                    {"  "}{plan.growing
                      ? "the minimums don't cover the interest — the balance grows"
                      : "at these payments"}
                  </Text>
                </Text>
              )}
              {guessed > 0 ? (
                <Text style={[s.mut, { marginTop: 4 }]}>
                  Estimated: {guessed === 1 ? "1 debt is" : `${guessed} debts are`}
                  {" "}walked at a typical rate or an estimated minimum
                  {viewer ? "."
                    : " — add the APR and minimum below for an exact date."}
                </Text>
              ) : null}
              <KV k="Paying monthly" v={m$(plan.monthly)} />
              <Text style={s.mut}>every minimum + {m$(plan.extra)} extra</Text>
              {finishes(plan) ? (<>
              <KV k={r.estimated ? "Interest to pay (estimated)" : "Interest to pay"}
                  v={m$(plan.total_interest)} />
              <Text style={[s.mut, savedVsMin !== null && savedVsMin > 0
                              && { color: C.good }]}>
                {savedVsMin !== null && savedVsMin > 0
                  ? (r.extra_monthly > 0
                    ? `saves ${m$(savedVsMin)} vs minimums only`
                    : `saves ${m$(savedVsMin)} by moving each paid-off minimum on`)
                  : base.debt_free
                    ? `minimums only: ${m$(base.total_interest)}`
                    : "minimums only never finish"}
              </Text>
              </>) : (<>
              <KV k="Interest to pay" v="no end" />
              <Text style={[s.mut, { color: C.bad }]}>{noEnd(plan)}</Text>
              </>)}
              <KV k={`${NAMES[OTHER[method]]} instead`}
                  v={alt.debt_free ? monthYear(alt.debt_free) : "never"} />
              {!finishes(alt) ? (
                <Text style={[s.mut, { color: C.bad }]}>{noEnd(alt)}</Text>
              ) : !finishes(plan) ? (
                <Text style={[s.mut, { color: C.good }]}>
                  {m$(alt.total_interest)} interest
                </Text>
              ) : (
              <Text style={[s.mut,
                            altMore !== null && altMore <= 0
                              && { color: C.good }]}>
                {m$(alt.total_interest)} interest
                {altMore !== null && altMore !== 0
                  ? (altMore > 0
                    ? ` — ${m$(altMore)} more`
                    : ` — ${m$(-altMore)} less`)
                  : ""}
              </Text>)}

              <View style={{ backgroundColor: C.hover, borderRadius: 8,
                             padding: 10, marginTop: 8, gap: 8 }}>
                <SegmentedControl label="Payoff method" value={method}
                  onChange={pick}
                  options={(["avalanche", "snowball"] as DebtMethod[]).map(
                    (m) => ({ value: m, label: NAMES[m] }))} />
                <View style={{ flexDirection: "row", gap: 8,
                               alignItems: "flex-end" }}>
                  <View style={{ width: 110 }}>
                    <Text style={{ color: C.mut, fontSize: 10 }}>extra $/mo</Text>
                    <TextInput {...fieldA11y("Extra $/mo")} style={s.input} keyboardType="numeric"
                               value={extraStr} onChangeText={setExtra} />
                  </View>
                  <Pressable accessibilityRole="button"
                             style={[s.btn, query.isFetching && { opacity: 0.5 }]}
                             disabled={query.isFetching}
                             onPress={() => setParams({ method, extra: extraQ })}>
                    <Text style={s.btnText}>
                      {query.isFetching ? "Recalculating…" : "Recalculate"}
                    </Text>
                  </Pressable>
                  {!viewer && (
                    <Pressable accessibilityRole="button" style={[s.btn, s.btnAlt,
                                       (save.isPending || !unsaved) && { opacity: 0.5 }]}
                               disabled={save.isPending || !unsaved}
                               onPress={persist}>
                      <Text style={s.btnText}>
                        {save.isPending ? "Saving…" : "Save plan"}
                      </Text>
                    </Pressable>
                  )}
                </View>
                <Text style={s.mut}>
                  {method === "avalanche"
                    ? "Avalanche pays the highest rate first — the least interest."
                    : "Snowball clears the smallest balance first — the earliest win."}
                  {" "}Every minimum keeps being paid; the extra, and each
                  retired debt&apos;s minimum, roll onto the next target.
                </Text>
                {whatIfErr
                  ? <Text style={[s.mut, { color: C.bad }]}>{whatIfErr}</Text>
                  : null}
                {err ? <Note bad style={[s.mut, { color: C.bad }]}>{err}</Note> : null}
              </View>
            </Card>

            {plan.series.length > 1 && (
              <Card>
                <H>
                  Balance owed{" "}
                  <Text style={[s.mut, { fontWeight: "400" }]}>
                    — {r.alt_same_path
                      ? `${NAMES[method].toLowerCase()} and ${NAMES[OTHER[method]].toLowerCase()} pay these debts down the same way`
                      : `${NAMES[method].toLowerCase()} vs ${NAMES[OTHER[method]].toLowerCase()}, same money`}
                  </Text>
                </H>
                <Sparkline width={width - 24 - 28} data={plan.series}
                           subject={`Balance owed, ${NAMES[method].toLowerCase()}`}
                           second={r.alt_same_path ? undefined : alt.series}
                           legend={[NAMES[method], NAMES[OTHER[method]]]} />
              </Card>
            )}

            <Card>
              <H>In payoff order</H>
              {ordered.map((x) => {
                const p = byId[x.id];
                const row = d[x.id];
                return (
                  <View key={x.id} style={[s.debt, x.skip && { opacity: 0.5 }]}>
                    <View style={{ flexDirection: "row", alignItems: "center",
                                   gap: 6 }}>
                      <Text style={[s.mut, { width: 18 }]}>{p ? p.order : "—"}</Text>
                      <Text style={{ color: C.text, fontSize: 14, flex: 1,
                                     fontWeight: "600" }}>
                        {x.name}
                        {x.mask ? <Text style={s.mut}> ···{x.mask}</Text> : null}
                      </Text>
                      <Text style={s.num}>{m$(x.balance)}</Text>
                    </View>
                    <View style={{ flexDirection: "row", alignItems: "center",
                                   gap: 6, marginLeft: 24 }}>
                      <Text style={[s.mut, { flex: 1 }]}>
                        {x.institution ? `${x.institution} · ` : ""}{x.kind}
                        {x.origin === "hand_entered" ? " · entered on Net Worth" : ""}
                      </Text>
                      {x.skip ? <Pill text="left out" />
                        : p?.paid_off ? <Pill text={monthYear(p.paid_off)} tone="good" />
                        : <Pill text="never" tone="bad" />}
                      <Text style={s.mut}>
                        {/* a debt the walk never clears has no interest total */}
                        {p?.paid_off ? `${m$(p.interest)} interest` : ""}
                      </Text>
                    </View>
                    <View style={{ flexDirection: "row", alignItems: "flex-end",
                                   gap: 8, marginLeft: 24, marginTop: 4 }}>
                      <View style={{ width: 90 }}>
                        <Text style={{ color: C.mut, fontSize: 10 }}>
                          APR % · {x.apr_source === "you" ? "yours"
                            : x.apr_source === "issuer" ? "issuer"
                            : x.rate_needed ? (viewer ? "rate not given yet"
                                               : "rate needed — type it once")
                            : viewer ? "est. — typical rate" : "est. — add yours"}
                        </Text>
                        {viewer
                          ? <Text style={s.val}>{x.apr.toFixed(2)}</Text>
                          : <TextInput {...fieldA11y(`${x.name} APR %`)}
                                       style={s.input} keyboardType="decimal-pad"
                                       placeholder={x.apr_default?.toFixed(2)}
                                       placeholderTextColor={C.mut}
                                       value={row?.apr ?? ""}
                                       onChangeText={(v) => setRow(x.id, { apr: v })} />}
                      </View>
                      <View style={{ width: 100 }}>
                        <Text style={{ color: C.mut, fontSize: 10 }}>
                          Min $/mo · {MIN_FROM[x.min_basis ?? x.min_source]}
                        </Text>
                        {viewer
                          ? <Text style={s.val}>{m$(x.min_payment)}</Text>
                          : <TextInput {...fieldA11y(`${x.name} minimum $/mo`)}
                                       style={s.input} keyboardType="numeric"
                                       placeholder={x.min_default?.toFixed(2)}
                                       placeholderTextColor={C.mut}
                                       value={row?.min_payment ?? ""}
                                       onChangeText={(v) =>
                                         setRow(x.id, { min_payment: v })} />}
                        {x.kind === "mortgage" && x.payment_reported != null
                          ? <Text style={s.mut}>
                              Reported payment incl. escrow{" "}
                              {m$(x.payment_reported)}
                            </Text>
                          : null}
                      </View>
                      <View style={{ flex: 1 }} />
                      {!viewer && (
                        <View style={{ alignItems: "center" }}>
                          <Text style={{ color: C.mut, fontSize: 10 }}>skip</Text>
                          <Switch value={row?.skip ?? false}
                                  accessibilityLabel={`Leave ${x.name} out of the plan`}
                                  onValueChange={(v) => setRow(x.id, { skip: v })}
                                  trackColor={{ true: C.accent }} />
                        </View>
                      )}
                    </View>
                  </View>
                );
              })}
              <Text style={[s.mut, { marginTop: 8 }]}>
                Rates and minimums are the bank&apos;s where it reports
                them, yours where you type them, otherwise an estimate.
                {!viewer && draft !== null
                  ? " Changes here apply when you save the plan." : ""}
              </Text>
            </Card>
          </>
        );
      })()}
      {r && (r.paid_in_full ?? []).length > 0 && (
        // cards cleared every statement: the server leaves them out of the
        // plan (web: PaidInFull in pages/Debt.tsx)
        <Card>
          <H>
            Paid in full monthly{" "}
            <Text style={[s.mut, { fontWeight: "400" }]}>
              — no interest, so not in the plan
            </Text>
          </H>
          {(r.paid_in_full ?? []).map((c) => (
            <View key={c.id} style={[s.debt, { flexDirection: "row", gap: 6 }]}>
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontSize: 14 }}>
                  {c.name}
                  {c.mask ? <Text style={s.mut}> ···{c.mask}</Text> : null}
                </Text>
                <Text style={s.mut}>
                  {c.basis === "issuer" && c.statement_balance !== null
                    ? `last statement ${m$(c.statement_balance)} paid in full`
                    : "your payments clear it each month"}
                </Text>
              </View>
              <Text style={s.num}>{m$(c.balance)}</Text>
            </View>
          ))}
        </Card>
      )}
      <HelpLink topic="debt" />
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  big: { color: C.text, fontSize: 26, fontWeight: "700",
         marginVertical: 4 },
  input: { backgroundColor: C.bg, borderColor: C.field, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 13,
           paddingHorizontal: 8, paddingVertical: 6 },
  val: { color: C.text, fontSize: 13, paddingVertical: 6 },
  btn: { backgroundColor: C.btn, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8 },
  btnAlt: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1 },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  debt: { borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
          paddingVertical: 8, gap: 2 },
  num: { color: C.text, fontSize: 13, textAlign: "right",
         fontVariant: ["tabular-nums"] },
});
