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
import StaleBanner from "../components/stale-banner";
import { Card, H, HelpLink, KV, Pill } from "../components/ui";
import { errText, whatIfRefusal, type DebtMethod, type DebtPlan, type DebtPlanSave,
         type DebtRow, type DebtSchedule } from "../lib/api";
import { moneyParam, monthYear, span } from "../lib/pure";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C, money } from "../lib/theme";

const m$ = (n: number) => money(n, false);
const NAMES: Record<DebtMethod, string> =
  { avalanche: "Avalanche", snowball: "Snowball" };
const OTHER: Record<DebtMethod, DebtMethod> =
  { avalanche: "snowball", snowball: "avalanche" };

type Draft = Record<string, { apr: string; min_payment: string; skip: boolean }>;

const draftOf = (rows: DebtRow[]): Draft =>
  Object.fromEntries(rows.map((d) => [d.id, {
    // the box shows what the household typed; an issuer/estimated value
    // sits in the placeholder so clearing the box means "back to that"
    apr: d.apr_source === "you" ? String(d.apr) : "",
    min_payment: d.min_source === "you" ? String(d.min_payment) : "",
    skip: d.skip,
  }]));

export default function Debt() {
  const { client } = useSession();
  const qc = useQueryClient();
  const { width } = useWindowDimensions();
  const viewer = useViewer();
  const [params, setParams] = useState<Record<string, string>>({});
  const [extra, setExtra] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const query = useQuery({
    queryKey: ["debt-plan", params],
    queryFn: () => client!.debtPlan(params),
    enabled: !!client,
    placeholderData: (prev) => prev,
  });
  const save = useMutation({
    mutationFn: (body: DebtPlanSave) => client!.debtPlanSave(body),
    onSuccess: (d) => {
      // the reply IS the saved plan: seed the unparameterised key and
      // drop the what-if so the screen reads what it just wrote
      qc.setQueryData(["debt-plan", {}], d);
      qc.invalidateQueries({ queryKey: ["debt-plan"] });
      setParams({}); setExtra(null); setDraft(null); setErr(null);
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
      ?? "Couldn't reach the server — pull down to retry."
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
  const setRow = (id: string, patch: Partial<Draft[string]>) =>
    setDraft({ ...d, [id]: { ...d[id], ...patch } });
  const persist = () => save.mutate({
    method, extra_monthly: extraQ,
    debts: Object.fromEntries(Object.entries(d).map(([id, v]) => [id, {
      apr: v.apr === "" ? null : v.apr,
      min_payment: v.min_payment === "" ? null : v.min_payment,
      skip: v.skip,
    }])),
  });

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      <StaleBanner query={query} />
      {query.isPending && !r && <Text style={s.center}>Loading…</Text>}
      {query.isError && !r && (
        <Text style={[s.center, { color: C.bad }]}>
          Couldn&apos;t reach the server — pull down to retry.
        </Text>
      )}
      {r && r.debts.length === 0 && (
        <Card>
          <Text style={[s.big, { color: C.good }]}>Nothing owed</Text>
          <Text style={s.mut}>
            No card or loan carries a balance. A debt on an account the
            Accounts page excludes stays out of the plan too.
          </Text>
        </Card>
      )}
      {r && r.debts.length > 0 && (() => {
        const plan = r.plan, alt = r.alt, base = r.minimums;
        const live = r.debts.filter((x) => !x.skip);
        const owed = live.reduce((sum, x) => sum + x.balance, 0);
        const byId = Object.fromEntries(plan.debts.map((x) => [x.id, x]));
        const ordered = [...r.debts].sort((a, b) =>
          (byId[a.id]?.order ?? 999) - (byId[b.id]?.order ?? 999));
        const saved = (x: DebtSchedule) => base.total_interest - x.total_interest;
        return (
          <>
            <Card>
              <View style={{ flexDirection: "row", alignItems: "center",
                             gap: 8, flexWrap: "wrap" }}>
                <Text style={s.mut}>
                  Debt-free — {NAMES[method].toLowerCase()}
                  {r.extra_monthly > 0
                    ? `, ${m$(r.extra_monthly)}/mo extra`
                    : ", minimums only rolled"}
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
                      ? "the minimums don't cover the interest"
                      : "at these payments"}
                  </Text>
                </Text>
              )}
              <KV k="Paying monthly" v={m$(plan.monthly)} />
              <Text style={s.mut}>every minimum + {m$(plan.extra)} extra</Text>
              <KV k="Interest to pay" v={m$(plan.total_interest)} />
              <Text style={[s.mut, saved(plan) > 0 && { color: C.good }]}>
                {saved(plan) > 0
                  ? `saves ${m$(saved(plan))} vs minimums only`
                  : base.debt_free
                    ? `minimums only: ${m$(base.total_interest)}`
                    : "minimums only never finish"}
              </Text>
              <KV k={`${NAMES[OTHER[method]]} instead`}
                  v={alt.debt_free ? monthYear(alt.debt_free) : "never"} />
              <Text style={[s.mut,
                            alt.total_interest <= plan.total_interest
                              && { color: C.good }]}>
                {m$(alt.total_interest)} interest
                {alt.total_interest !== plan.total_interest
                  ? (alt.total_interest > plan.total_interest
                    ? ` — ${m$(alt.total_interest - plan.total_interest)} more`
                    : ` — ${m$(plan.total_interest - alt.total_interest)} less`)
                  : ""}
              </Text>

              <View style={{ backgroundColor: C.hover, borderRadius: 8,
                             padding: 10, marginTop: 8, gap: 8 }}>
                <View style={{ flexDirection: "row", gap: 6 }}>
                  {(["avalanche", "snowball"] as DebtMethod[]).map((m) => (
                    <Pressable key={m} onPress={() => pick(m)}
                               style={[s.seg, method === m && s.segOn]}>
                      <Text style={[s.segText,
                                    method === m && { color: C.text }]}>
                        {NAMES[m]}
                      </Text>
                    </Pressable>
                  ))}
                </View>
                <View style={{ flexDirection: "row", gap: 8,
                               alignItems: "flex-end" }}>
                  <View style={{ width: 110 }}>
                    <Text style={{ color: C.mut, fontSize: 10 }}>extra $/mo</Text>
                    <TextInput style={s.input} keyboardType="numeric"
                               value={extraStr} onChangeText={setExtra} />
                  </View>
                  <Pressable style={[s.btn, query.isFetching && { opacity: 0.5 }]}
                             disabled={query.isFetching}
                             onPress={() => setParams({ method, extra: extraQ })}>
                    <Text style={s.btnText}>
                      {query.isFetching ? "Recalculating…" : "Recalculate"}
                    </Text>
                  </Pressable>
                  {!viewer && (
                    <Pressable style={[s.btn, s.btnAlt,
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
                {err ? <Text style={[s.mut, { color: C.bad }]}>{err}</Text> : null}
              </View>
            </Card>

            {plan.series.length > 1 && (
              <Card>
                <H>
                  Balance owed{" "}
                  <Text style={[s.mut, { fontWeight: "400" }]}>
                    — {NAMES[method].toLowerCase()} vs{" "}
                    {NAMES[OTHER[method]].toLowerCase()}, same money
                  </Text>
                </H>
                <Sparkline width={width - 24 - 28} data={plan.series}
                           second={alt.series}
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
                      </Text>
                      {x.skip ? <Pill text="left out" />
                        : p?.paid_off ? <Pill text={monthYear(p.paid_off)} tone="good" />
                        : <Pill text="never" tone="bad" />}
                      <Text style={s.mut}>
                        {p ? `${m$(p.interest)} interest` : ""}
                      </Text>
                    </View>
                    <View style={{ flexDirection: "row", alignItems: "flex-end",
                                   gap: 8, marginLeft: 24, marginTop: 4 }}>
                      <View style={{ width: 90 }}>
                        <Text style={{ color: C.mut, fontSize: 10 }}>
                          APR % · {x.apr_source === "you" ? "yours"
                            : x.apr_source === "issuer" ? "issuer" : "unknown"}
                        </Text>
                        {viewer
                          ? <Text style={s.val}>{x.apr.toFixed(2)}</Text>
                          : <TextInput style={s.input} keyboardType="decimal-pad"
                                       placeholder={x.apr_issuer != null
                                         ? x.apr_issuer.toFixed(2) : "0"}
                                       placeholderTextColor={C.mut}
                                       value={row?.apr ?? ""}
                                       onChangeText={(v) => setRow(x.id, { apr: v })} />}
                      </View>
                      <View style={{ width: 100 }}>
                        <Text style={{ color: C.mut, fontSize: 10 }}>
                          Min $/mo · {x.min_source === "you" ? "yours"
                            : x.min_source === "issuer" ? "issuer" : "estimate"}
                        </Text>
                        {viewer
                          ? <Text style={s.val}>{m$(x.min_payment)}</Text>
                          : <TextInput style={s.input} keyboardType="numeric"
                                       placeholder={String(Math.round(
                                         x.min_issuer ?? x.min_payment))}
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
                                  onValueChange={(v) => setRow(x.id, { skip: v })}
                                  trackColor={{ true: C.accent }} />
                        </View>
                      )}
                    </View>
                  </View>
                );
              })}
              <Text style={[s.mut, { marginTop: 8 }]}>
                Rates and minimums come from the bank where it reports them,
                else from what you type here; an estimate is 2% of a
                card&apos;s balance (at least $25), a five-year payment for a
                loan, thirty years for a mortgage — correct it and save.
                Minimums are held flat for the whole walk, and interest
                accrues monthly at APR ÷ 12.
                {!viewer && draft !== null
                  ? " Changes here apply when you save the plan." : ""}
              </Text>
            </Card>
          </>
        );
      })()}
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
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 13,
           paddingHorizontal: 8, paddingVertical: 6 },
  val: { color: C.text, fontSize: 13, paddingVertical: 6 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8 },
  btnAlt: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1 },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  seg: { borderColor: C.border, borderWidth: 1, borderRadius: 999,
         paddingHorizontal: 12, paddingVertical: 5 },
  segOn: { backgroundColor: C.accent, borderColor: C.accent },
  segText: { color: C.mut, fontSize: 13, fontWeight: "600" },
  debt: { borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
          paddingVertical: 8, gap: 2 },
  num: { color: C.text, fontSize: 13, textAlign: "right",
         fontVariant: ["tabular-nums"] },
});
