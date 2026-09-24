// Retirement — the full web model: earliest-age hero with the three
// tiles, the assumptions line with the what-if ADJUST form (every
// parameter recalculates server-side), the projected-portfolio chart,
// tax treatment, the historical replay, the stress table, and
// retire-at-each-age with the history column.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams } from "expo-router";
import { useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View,
         useWindowDimensions } from "react-native";
import PullRefresh from "../components/pull-refresh";

import Sparkline from "../components/sparkline";
import StaleBanner from "../components/stale-banner";
import { Card, H, HelpLink, KV, Pill } from "../components/ui";
import { whatIfRefusal } from "../lib/api";
import { saveSettings } from "../lib/cache";
import { numv, whatIfQuery } from "../lib/pure";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C, money } from "../lib/theme";

const m$ = (n: number) => money(n, false);
// the dollar boxes: /api/retirement takes them as whole numbers, so
// "60,000" or "250.50" from a phone keyboard is parsed before it rides
const MONEY_KEYS = ["spend", "employer_mo", "taxable_mo"] as const;
const whatIfParams = (f: Record<string, string>) =>
  whatIfQuery(f, MONEY_KEYS, true);

function Fld({ label, value, placeholder, onChange }: {
  label: string; value: string;
  // what an empty field means (the server's default), shown in its place
  placeholder?: string;
  onChange: (v: string) => void;
}) {
  return (
    <View style={{ width: "30%" }}>
      <Text style={{ color: C.mut, fontSize: 10 }}>{label}</Text>
      <TextInput style={s.input} keyboardType="numbers-and-punctuation"
                 placeholder={placeholder} placeholderTextColor={C.mut}
                 value={value} onChangeText={onChange} />
    </View>
  );
}

export default function Retire() {
  const { client } = useSession();
  const qc = useQueryClient();
  const { width } = useWindowDimensions();
  const [params, setParams] = useState<Record<string, string>>({});
  const [adjust, setAdjust] = useState(false);
  const [form, setForm] =
    useState<Record<string, string> | null>(null);
  const query = useQuery({
    queryKey: ["retirement", params],
    queryFn: () => client!.retirement(params),
    enabled: !!client,
    placeholderData: (prev) => prev,
  });
  // A failed what-if leaves query.data empty (placeholderData only covers
  // the pending fetch), and the whole page renders from it. Keep the last
  // projection the server did send, so a refused answer can be corrected
  // in the form instead of taking the screen with it.
  const [lastGood, setLastGood] = useState<typeof query.data>();
  if (query.data && query.data !== lastGood) setLastGood(query.data);
  // first visit with no birthdate and no wizard mark → offer the
  // walkthrough; "done" OR "skipped" both mean finished
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const sp = useLocalSearchParams<{ wizard?: string }>();
  const [wizard, setWizard] =
    useState<boolean | null>(sp.wizard === "1" ? true : null);
  const marks = settings.data?.ret_wizard_steps as
    { done?: string } | undefined;
  // never for a viewer (the web's rule): the wizard's exits write the
  // mark, a viewer 403s — they see the page as it stands
  const viewer = useViewer();
  const needsWizard = wizard === null && !viewer
    && settings.data !== undefined
    && !marks?.done && !settings.data.birthdate
    && Object.keys(params).length === 0;
  if ((wizard === true || needsWizard) && query.data) {
    return (
      <RetireWizard
        defaults={query.data.inputs}
        // the mark seeds ["settings"] from the reply: needsWizard reads
        // it, and a stale copy would re-show the wizard on the next visit
        onDone={(p) => {
          saveSettings(qc, client!, { ret_wizard_steps: { done: "done" } })
            .catch(() => {});
          setParams(p); setWizard(false); setForm(null);
        }}
        onSkip={() => {
          saveSettings(qc, client!, { ret_wizard_steps: { done: "skipped" } })
            .catch(() => {});
          setWizard(false);
        }} />
    );
  }
  const r = query.data ?? lastGood;
  const whatIfErr = query.isError && !query.data
    ? whatIfRefusal(query.error)
      ?? "Couldn't reach the server — pull down to retry."
    : null;
  const inp = r?.inputs;
  const f = form ?? (inp ? {
    // age MUST ride the recalc — dropping it re-projects from the
    // server's derived age and silently discards a wizard answer
    age: String(inp.age),
    spend: String(inp.spend), ret: inp.ret.toFixed(1),
    infl: inp.infl.toFixed(1), end: String(inp.end),
    ssage: String(inp.ssage), stockpct: String(inp.stockpct),
    employer_mo: String(inp.employer_mo),
    taxable_mo: String(inp.taxable_mo), resume: String(inp.resume),
    // blank = the server's default (cash keeps pace with inflation)
    cash: inp.cash == null ? "" : inp.cash.toFixed(1),
  } : null);
  const set = (k: string) => (v: string) =>
    setForm({ ...(f ?? {}), [k]: v });
  const can = r?.earliest ?? null;
  const lastAge = r?.rows[r.rows.length - 1]?.age;
  const worst = r
    ? Math.min(...r.stress.filter((x) => x.label !== "Average markets")
        .map((x) => x.max_spend_at_ref))
    : 0;
  const margin = r ? worst - r.spend : 0;

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
      {r && inp && f && (
        <>
          <Card>
            <Text style={s.mut}>
              Earliest retirement at {m$(r.spend)}/yr
            </Text>
            {can !== null ? (
              <Text style={s.big}>
                age {can}
                <Text style={{ color: C.good, fontSize: 13,
                               fontWeight: "400" }}>
                  {"  "}feasible — money lasts to {inp.end}
                </Text>
              </Text>
            ) : (
              <Text style={[s.big, { color: C.bad }]}>
                not by {lastAge}
                <Text style={{ fontSize: 13, fontWeight: "400" }}>
                  {"  "}not sustainable without more saving
                </Text>
              </Text>
            )}
            {can === null && (r.catch_up ?? []).length > 0 ? (
              <Text style={[s.mut, { marginTop: 4 }]}>
                <Text style={{ color: C.text }}>
                  To get back on track, save{" "}
                  {(r.catch_up ?? []).map((c, k) => (
                    <Text key={c.age}>
                      {k > 0 ? ", or " : ""}
                      <Text style={{ fontWeight: "700" }}>{m$(c.extra_monthly)}/mo more</Text>
                      {" "}to retire at {c.age}
                    </Text>
                  ))}
                </Text>
                {" "}— starting now, on top of what the plan already saves.
              </Text>
            ) : null}
            <KV k={`Retire today (${inp.age}) supports`}
                v={`${m$(r.spend_now)}/yr — after-tax, today's dollars`} />
            <KV k="You have today"
                v={`${m$(r.buckets.total)} → ~${m$(r.grow_to_67)} by 67`} />
            <Text style={s.mut}>
              at {(r.real_return * 100).toFixed(1)}%/yr real
              {!inp.saving_yr ? " (saving nothing)" : ""}
            </Text>
            <KV k={`Crash-proof spend at ${r.stress_ref_age}`}
                v={`${m$(worst)}/yr`}
                tone={margin >= 0 ? "good" : "bad"} />
            <Text style={s.mut}>
              {margin >= 0 ? "+" : "−"}{m$(Math.abs(margin))} vs target —
              survives the worst stress scenario
            </Text>
            <Text style={[s.mut, { marginTop: 8 }]}>
              <Text style={{ color: C.text, fontWeight: "700" }}>
                Assumptions:{" "}
              </Text>
              {inp.ret}% return · {inp.infl}% inflation
              {inp.cash != null ? ` · cash ${inp.cash}%` : ""} · to {inp.end} ·
              SS at {inp.ssage}
              {inp.has_sched ? ` (${m$(inp.your_ss)}/mo)`
                             : " (no SSA statement)"}
              {" · "}{inp.stockpct}% stocks ·{" "}
              {inp.saving_yr
                ? `saving ${m$(inp.saving_yr)}/yr`
                  + (inp.resume > inp.age ? ` from ${inp.resume}` : "")
                : "saving paused"}
              {"   "}
              <Text style={{ color: C.accent }}
                    onPress={() => setAdjust((a) => !a)}>
                {adjust ? "done ▴" : "adjust ▾"}
              </Text>
              {"   "}
              <Text style={{ color: C.mut }}
                    onPress={() => setWizard(true)}>
                re-run wizard
              </Text>
            </Text>
            {adjust && (
              <View style={{ backgroundColor: C.hover, borderRadius: 8,
                             padding: 10, marginTop: 8, gap: 8 }}>
                <View style={{ flexDirection: "row", flexWrap: "wrap",
                               gap: 8 }}>
                  <Fld label="spend $/yr" value={f.spend}
                       onChange={set("spend")} />
                  <Fld label="return %" value={f.ret}
                       onChange={set("ret")} />
                  <Fld label="inflation %" value={f.infl}
                       onChange={set("infl")} />
                  <Fld label="cash yield %" value={f.cash}
                       placeholder="= inflation" onChange={set("cash")} />
                  <Fld label="to age" value={f.end}
                       onChange={set("end")} />
                  <Fld label="SS claim" value={f.ssage}
                       onChange={set("ssage")} />
                  <Fld label="stock %" value={f.stockpct}
                       onChange={set("stockpct")} />
                  <Fld label="employer $/mo" value={f.employer_mo}
                       onChange={set("employer_mo")} />
                  <Fld label="taxable $/mo" value={f.taxable_mo}
                       onChange={set("taxable_mo")} />
                  <Fld label="resume at" value={f.resume}
                       onChange={set("resume")} />
                </View>
                <Pressable style={[s.btn, { alignSelf: "flex-start" },
                                   query.isFetching && { opacity: 0.5 }]}
                           disabled={query.isFetching}
                           // a blank field is "use the default" and must
                           // not ride the query as an empty value
                           onPress={() => setParams(whatIfParams(f))}>
                  <Text style={s.btnText}>
                    {query.isFetching ? "Recalculating…" : "Recalculate"}
                  </Text>
                </Pressable>
              </View>
            )}
            {whatIfErr
              ? <Text style={[s.mut, { color: C.bad, marginTop: 6 }]}>
                  {whatIfErr}
                </Text>
              : null}
          </Card>

          {r.growth_path.length > 1 && (
            <Card>
              <H>
                Projected portfolio{" "}
                <Text style={[s.mut, { fontWeight: "400" }]}>
                  — today&apos;s dollars
                </Text>
              </H>
              <Sparkline width={width - 24 - 28} data={r.growth_path} />
            </Card>
          )}

          <Card>
            <H>By tax treatment</H>
            <KV k="Tax-deferred (401k/403b/457/Trad IRA)" v={m$(r.buckets.td)} />
            <Text style={s.mut}>income tax on withdrawal</Text>
            <KV k="Roth" v={m$(r.buckets.roth)} />
            <Text style={s.mut}>tax-free</Text>
            <KV k="Taxable + crypto" v={m$(r.buckets.taxable)} />
            <Text style={s.mut}>capital gains</Text>
            <KV k="Cash" v={m$(r.buckets.cash)} />
          </Card>

          {r.hist && (
            <Card>
              <H>
                Replayed against history{" "}
                <Text style={[s.mut, { fontWeight: "400" }]}>
                  — 1928–2025, {Math.round(r.hist.stock_frac * 100)}/
                  {Math.round((1 - r.hist.stock_frac) * 100)} stocks/bonds
                </Text>
              </H>
              <KV k={`Retiring at ${r.stress_ref_age} on target`}
                  v={`${r.hist.pct.toFixed(1)}%`}
                  tone={r.hist.pct >= 90 ? "good"
                    : r.hist.pct < 75 ? "bad" : undefined} />
              <Text style={s.mut}>
                survived {r.hist.ok} of {r.hist.n} historical sequences
              </Text>
              <KV k="Spend with 90% historical success"
                  v={`${m$(r.hist.spend90)}/yr`} />
              <KV k="Spend that survived ALL of history"
                  v={`${m$(r.hist.spend100)}/yr`} />
              <Text style={s.mut}>
                Every contiguous return sequence since 1928 (real S&amp;P +
                10y Treasury, wrapping), your exact tax-aware draw order
                and RMDs — deterministic, not simulated randomness.
              </Text>
            </Card>
          )}

          <Card>
            <H>
              Stress test{" "}
              <Text style={[s.mut, { fontWeight: "400" }]}>
                — a bad market the day you retire
              </Text>
            </H>
            <View style={s.row}>
              <Text style={[s.head, { flex: 1, textAlign: "left" }]}>
                Scenario
              </Text>
              <Text style={s.head}>Earliest</Text>
              <Text style={s.head}>
                Max at {r.stress_ref_age}
              </Text>
            </View>
            {r.stress.map((x) => (
              <View key={x.label} style={s.row}>
                <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
                  {x.label}
                </Text>
                {x.earliest !== null
                  ? <Pill text={`age ${x.earliest}`} tone="good" />
                  : <Pill text={`not by ${lastAge}`} tone="bad" />}
                <Text style={[s.num,
                  x.max_spend_at_ref < r.spend && { color: C.bad }]}>
                  {m$(x.max_spend_at_ref)}
                </Text>
              </View>
            ))}
          </Card>

          <Card>
            <H>Retire at each age</H>
            <View style={s.row}>
              <Text style={[s.head, { width: 42, textAlign: "left" }]}>
                Age
              </Text>
              <Text style={[s.head, { flex: 1 }]}>Portfolio</Text>
              <Text style={[s.head, { width: 40 }]}>OK?</Text>
              <Text style={s.head}>Max spend</Text>
              <Text style={[s.head, { width: 52 }]}>History</Text>
            </View>
            {r.rows.map((row) => (
              <View key={row.age}
                    style={[s.row, row.age === can
                      && { backgroundColor: "rgba(92,181,107,.10)" }]}>
                <Text style={{ color: C.text, fontSize: 12, width: 42 }}>
                  {row.age}
                  {row.age === can ? (
                    <Text style={{ color: C.good, fontSize: 9 }}>
                      {"  "}earliest
                    </Text>
                  ) : null}
                </Text>
                <Text style={[s.num, { flex: 1 }]}>
                  {m$(row.at_retire)}
                </Text>
                <Text style={{ width: 40, textAlign: "right",
                               fontSize: 12,
                               color: row.feasible ? C.good : C.bad }}>
                  {row.feasible ? "yes"
                    : row.fail_age
                      ? `no · runs out at ${row.fail_age}` : "no"}
                </Text>
                <Text style={[s.num, row.max_spend >= r.spend
                               && { color: C.good }]}>
                  {m$(row.max_spend)}
                </Text>
                <Text style={[s.num, { width: 52 },
                              (row.hist_pct ?? 0) >= 90
                                ? { color: C.good }
                                : (row.hist_pct ?? 0) < 75
                                  ? { color: C.bad } : null]}>
                  {row.hist_pct !== null
                    ? `${row.hist_pct.toFixed(1)}%` : "·"}
                </Text>
              </View>
            ))}
            <Text style={[s.mut, { marginTop: 6 }]}>
              Today&apos;s dollars, after tax, drawn cash → taxable →
              tax-deferred → Roth; Social Security from your claim age;
              RMDs from 72, 73 or 75 by birth year (IRS Uniform Lifetime
              Table), excess reinvested taxable. “Max spend” is the
              largest annual draw that just lasts to {inp.end}.
              “History” is the share of every return sequence since 1928
              that survived.
            </Text>
          </Card>
        </>
      )}
    </ScrollView>
  );
}

// the 5-step walkthrough — only birthdate persists; everything else
// rides the query params so /api/retirement actually reads the answers
function RetireWizard({ defaults, onDone, onSkip }: {
  defaults: { age: number; spend: number; employer_mo: number;
              taxable_mo: number; ret: number; infl: number;
              end: number };
  onDone: (p: Record<string, string>) => void;
  onSkip: () => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [i, setI] = useState(0);
  const [age, setAge] = useState(String(defaults.age || 40));
  const [birth, setBirth] = useState("");
  const [spend, setSpend] = useState(String(defaults.spend || 60000));
  const [employer, setEmployer] =
    useState(String(defaults.employer_mo || 0));
  const [taxable, setTaxable] =
    useState(String(defaults.taxable_mo || 0));
  const [ret, setRet] = useState(String(defaults.ret || 7));
  const [infl, setInfl] = useState(String(defaults.infl || 3));
  const [end, setEnd] = useState(String(defaults.end || 95));
  const [err, setErr] = useState<string | null>(null);
  const STEPS = ["How old are you?",
                 "What do you want to spend in retirement?",
                 "How much are you saving each month?",
                 "A few market assumptions", "You're set"];
  // A cleared box means "use the default", so it is left out of the query
  // rather than sent as an empty value — the adjust panel does the same.
  const params = () => whatIfParams({
    age, spend, employer_mo: employer, taxable_mo: taxable, ret, infl, end });
  const next = () => {
    setErr(null);
    if (i === 0) {
      const a = Number(age);
      if (!Number.isFinite(a) || a < 18 || a > 100) {
        setErr("Enter an age between 18 and 100."); return;
      }
    }
    // "60,000" is a spend, not a typo: judge it as the query will send it
    const spendN = Number(spend.replace(/[$,\s]/g, ""));
    if (i === 1 && (!Number.isFinite(spendN) || spendN < 0)) {
      setErr("Enter a non-negative annual spend."); return;
    }
    if (i === 3 && birth.trim())
      saveSettings(qc, client!, { birthdate: birth.trim() }).catch(() => {});
    setI(i + 1);
  };
  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ padding: 12 }}>
      <Text style={{ color: C.mut, fontSize: 12 }}>
        Step {i + 1} of 5
      </Text>
      <Text style={{ color: C.text, fontSize: 18, fontWeight: "700",
                     marginTop: 4 }}>
        {STEPS[i]}
      </Text>
      <View style={{ gap: 8, marginTop: 10 }}>
        {i === 0 && (<>
          <Text style={s.mut}>
            A short walkthrough so the model starts from where you are,
            not blank assumptions. You can skip and edit everything
            later.
          </Text>
          <Text style={s.mut}>
            We use this (and optionally a birthdate) as the starting age
            for the plan.
          </Text>
          <TextInput style={s.input} keyboardType="number-pad"
                     placeholder="current age" placeholderTextColor={C.mut}
                     value={age} onChangeText={setAge} />
          <TextInput style={s.input} autoCapitalize="none"
                     placeholder="birthdate (optional, YYYY-MM-DD)"
                     placeholderTextColor={C.mut}
                     value={birth} onChangeText={setBirth} />
        </>)}
        {i === 1 && (<>
          <Text style={s.mut}>
            Annual spending in today&apos;s dollars — housing, food,
            travel, the whole lifestyle.
          </Text>
          <TextInput style={s.input} keyboardType="number-pad"
                     placeholder="target spend $/year"
                     placeholderTextColor={C.mut}
                     value={spend} onChangeText={setSpend} />
        </>)}
        {i === 2 && (<>
          <Text style={s.mut}>
            Split employer retirement accounts (401k/403b) and taxable
            brokerage if you like.
          </Text>
          <TextInput style={s.input} keyboardType="number-pad"
                     placeholder="employer retirement $/mo"
                     placeholderTextColor={C.mut}
                     value={employer} onChangeText={setEmployer} />
          <TextInput style={s.input} keyboardType="number-pad"
                     placeholder="taxable saving $/mo"
                     placeholderTextColor={C.mut}
                     value={taxable} onChangeText={setTaxable} />
        </>)}
        {i === 3 && (<>
          <Text style={s.mut}>
            Sensible defaults — change them if you already have a view.
          </Text>
          <View style={{ flexDirection: "row", gap: 6 }}>
            <TextInput style={[s.input, { flex: 1 }]}
                       keyboardType="decimal-pad"
                       placeholder="return %" placeholderTextColor={C.mut}
                       value={ret} onChangeText={setRet} />
            <TextInput style={[s.input, { flex: 1 }]}
                       keyboardType="decimal-pad"
                       placeholder="inflation %"
                       placeholderTextColor={C.mut}
                       value={infl} onChangeText={setInfl} />
            <TextInput style={[s.input, { flex: 1 }]}
                       keyboardType="number-pad"
                       placeholder="to age" placeholderTextColor={C.mut}
                       value={end} onChangeText={setEnd} />
          </View>
        </>)}
        {i === 4 && (
          <Text style={{ color: C.text, fontSize: 14, lineHeight: 21 }}>
            Age <Text style={{ fontWeight: "700" }}>{age}</Text> · spend{" "}
            <Text style={{ fontWeight: "700" }}>
              ${numv(spend).toLocaleString()}/yr
            </Text> · saving{" "}
            <Text style={{ fontWeight: "700" }}>
              ${(numv(employer)
                 + numv(taxable)).toLocaleString()}/mo
            </Text>
            {" · return "}{ret}% / inflation {infl}% · to age {end}.
          </Text>
        )}
        {i === 4 && (
          <Text style={s.mut}>
            We&apos;ll open the full retirement model with these numbers.
            Tweak anytime.
          </Text>
        )}
        {err ? (
          <Text style={{ color: C.bad, fontSize: 12 }}>{err}</Text>
        ) : null}
        <View style={{ flexDirection: "row", gap: 8 }}>
          {i > 0 && i < 4 && (
            <Pressable style={[s.btn, { backgroundColor: C.hover }]}
                       onPress={() => { setErr(null); setI(i - 1); }}>
              <Text style={[s.btnText, { color: C.mut }]}>← Back</Text>
            </Pressable>
          )}
          {i < 4 ? (
            <Pressable style={s.btn} onPress={next}>
              <Text style={s.btnText}>
                {i === 3 ? "Finish →" : "Continue →"}
              </Text>
            </Pressable>
          ) : (
            <Pressable style={s.btn} onPress={() => onDone(params())}>
              <Text style={s.btnText}>Open my plan →</Text>
            </Pressable>
          )}
        </View>
        {i < 4 && (
          <Text style={{ color: C.mut, fontSize: 12 }}
                onPress={() => {
                  // Skip abandons the remaining questions, not a typed
                  // answer — a birthdate already entered persists
                  // exactly as finishing the wizard would have saved it
                  if (birth.trim())
                    saveSettings(qc, client!, { birthdate: birth.trim() })
                      .catch(() => {});
                  onSkip();
                }}>
            Skip — open the full model
          </Text>
        )}
      </View>
      <HelpLink topic="retirement" />
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
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8 },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  row: { flexDirection: "row", alignItems: "center", gap: 6,
         borderTopColor: C.border,
         borderTopWidth: StyleSheet.hairlineWidth,
         paddingVertical: 5 },
  num: { color: C.text, fontSize: 12, width: 70, textAlign: "right",
         fontVariant: ["tabular-nums"] },
  head: { color: C.mut, fontSize: 10, width: 70, textAlign: "right" },
});
