// Retirement — the full web model: earliest-age hero with the three
// tiles, the assumptions line with the what-if ADJUST form (every
// parameter recalculates server-side), the projected-portfolio chart,
// tax treatment, the historical replay, the stress table, and
// retire-at-each-age with the history column.
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams } from "expo-router";
import { type MutableRefObject, useEffect, useRef, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View,
         useWindowDimensions } from "react-native";
import PullRefresh from "../components/pull-refresh";

import Sparkline from "../components/sparkline";
import { FailedLine, StaleLine } from "../components/loaded";
import { Card, H, HelpLink, KV, Pill, Skeleton, Note } from "../components/ui";
import { ApiError, errText, whatIfRefusal } from "../lib/api";
import { saveSettings } from "../lib/cache";
import { rebaseSsDraft, ssDraftOf } from "../lib/ss-draft";
import { SS_CLAIM_AGES, ssEstimatesBody } from "../lib/ss-estimates";
import { birthdateIso, numv, parseMoneyDraft, RET_WIZARD_BOUNDS,
         RET_WIZARD_STEPS,
         retWizardMidway, retWizardPrefill, retWizardResumeAt,
         keyRetireAges, retWizardStepBody, SAVE_WAITS_FAILED,
         SAVE_WAITS_LOADING, failLine, whatIfQuery } from "../lib/pure";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C, money } from "../lib/theme";
import { fieldA11y } from "../components/field";

const m$ = (n: number) => money(n, false);
// where the projection's spend and saving came from, as the assumptions
// line words it (web: pages/Retirement.tsx SPEND_FROM / SAVING_FROM)
const SPEND_FROM: Record<string, string> = {
  what_if: "what-if", saved: "your answer", ledger: "your last 12 months",
  needed: "not set",
};
const SAVING_FROM: Record<string, string> = {
  what_if: "what-if", saved: "your answer", ledger: "from your last 12 months",
  none: "none entered",
};
// the dollar boxes: /api/retirement takes them as whole numbers, so
// "60,000" or "250.50" from a phone keyboard is parsed before it rides
const MONEY_KEYS = ["spend", "employer_mo", "roth_mo", "taxable_mo"] as const;
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
      <TextInput {...fieldA11y(label)} style={s.input} keyboardType="numbers-and-punctuation"
                 placeholder={placeholder} placeholderTextColor={C.mut}
                 value={value} onChangeText={onChange} />
    </View>
  );
}

// Unlike the what-if fields above it, this one is saved: the estimates are
// the projection's only source of Social Security income.
function SsEstimates({ saved }: { saved: Record<string, number> | null }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const seeded = ssDraftOf(saved);
  const [draft, setDraft] = useState(seeded);
  // the schedule the untouched boxes show; a newer one moves them over
  const [base, setBase] = useState(seeded);
  if (JSON.stringify(base) !== JSON.stringify(seeded)) {
    setBase(seeded);
    setDraft(rebaseSsDraft(draft, base, seeded));
  }
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const save = () => {
    setDone(false);
    const b = ssEstimatesBody(draft, parseMoneyDraft);
    if (!b.ok) { setErr(b.error); return; }
    setErr(null);
    setBusy(true);
    saveSettings(qc, client!, { ss_estimates: b.body })
      .then(() => {
        setDone(true);
        // what went up is now what the boxes were based on: the server's
        // copy replaces it, and anything typed since stays
        setBase(draft);
        qc.invalidateQueries({ queryKey: ["retirement"] });
      })
      .catch((x) => setErr(errText(x)))
      .finally(() => setBusy(false));
  };
  return (
    <View style={{ backgroundColor: C.hover, borderRadius: 8,
                   padding: 10, marginTop: 8, gap: 8 }}>
      <Text style={s.mut}>Social Security $/mo by claim age</Text>
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
        {SS_CLAIM_AGES.map((age) => (
          <Fld key={age} label={`at ${age}`} value={draft[age] ?? ""}
               onChange={(v) => {
                 setDone(false);
                 setDraft((d) => ({ ...d, [age]: v }));
               }} />
        ))}
      </View>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <Pressable accessibilityRole="button" style={[s.btn, busy && { opacity: 0.5 }]}
                   disabled={busy} onPress={save}>
          <Text style={s.btnText}>{busy ? "Saving…" : "Save"}</Text>
        </Pressable>
        {done ? <Note style={{ color: C.good, fontSize: 12 }}>Saved</Note> : null}
      </View>
      {err ? <Note bad style={[s.mut, { color: C.bad }]}>{err}</Note> : null}
    </View>
  );
}

// No saved spend and too little history to read one from: the plan needs
// one before it projects anything (web: pages/Retirement.tsx SpendAsk).
// Saved with the walkthrough's answers, so every device opens on it.
function SpendAsk({ onWizard }: { onWizard: () => void }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [v, setV] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const save = () => {
    const n = parseMoneyDraft(v);
    if (n === null) { setErr("Enter an amount, like 60,000."); return; }
    setErr(null);
    setBusy(true);
    saveSettings(qc, client!, { ret_wizard_answers: { spend: n }, ret_answers_v: 2 })
      .then(() => qc.invalidateQueries({ queryKey: ["retirement"] }))
      .catch((x) => setErr(errText(x)))
      .finally(() => setBusy(false));
  };
  return (
    <View style={{ gap: 8, marginTop: 8 }}>
      <TextInput {...fieldA11y("Spend $/yr in retirement")}
                 style={s.input} keyboardType="numbers-and-punctuation"
                 placeholder="spend $/yr in retirement"
                 placeholderTextColor={C.mut} value={v} onChangeText={setV} />
      <View style={{ flexDirection: "row", alignItems: "center", gap: 12 }}>
        <Pressable accessibilityRole="button" style={[s.btn, busy && { opacity: 0.5 }]}
                   disabled={busy} onPress={save}>
          <Text style={s.btnText}>{busy ? "Saving…" : "Plan with this"}</Text>
        </Pressable>
        <Text style={{ color: C.accent }} onPress={onWizard}>
          or answer the walkthrough</Text>
      </View>
      {err ? <Note bad style={[s.mut, { color: C.bad }]}>{err}</Note> : null}
    </View>
  );
}

// No saved saving answer: the projection counts no new saving (the
// server's "none") and the screen asks for one, the spend ask's pattern
// (web: pages/Retirement.tsx SavingAsk). Blank box = $0 there.
function SavingAsk({ withRoth }: { withRoth: boolean }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [emp, setEmp] = useState("");
  const [roth, setRoth] = useState("");
  const [tax, setTax] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const save = () => {
    const blank = (v: string) => v.replace(/[$\s]/g, "") === "";
    if (blank(emp) && (!withRoth || blank(roth)) && blank(tax)) {
      setErr("Enter an amount, like 500."); return;
    }
    const e1 = blank(emp) ? 0 : parseMoneyDraft(emp);
    const r1 = blank(roth) ? 0 : parseMoneyDraft(roth);
    const t1 = blank(tax) ? 0 : parseMoneyDraft(tax);
    if (e1 === null || r1 === null || t1 === null) {
      setErr("Enter an amount, like 500."); return;
    }
    setErr(null);
    setBusy(true);
    // the Roth figure rides beside the answers: an older server refuses a
    // whole save over an answer key it does not know
    saveSettings(qc, client!,
                 { ret_wizard_answers: { employer_mo: e1, taxable_mo: t1 },
                    ...(withRoth ? { ret_wizard_roth_mo: r1 } : {}),
                    ret_answers_v: 2 })
      .then(() => qc.invalidateQueries({ queryKey: ["retirement"] }))
      .catch((x) => setErr(errText(x)))
      .finally(() => setBusy(false));
  };
  return (
    <View style={{ gap: 8, marginTop: 8 }}>
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8 }}>
        <Fld label="employer retirement $/mo" value={emp} onChange={setEmp} />
        {withRoth ? (
          <Fld label="Roth retirement $/mo" value={roth} onChange={setRoth} />
        ) : null}
        <Fld label="taxable saving $/mo" value={tax} onChange={setTax} />
      </View>
      <Pressable accessibilityRole="button" style={[s.btn, { alignSelf: "flex-start" },
                         busy && { opacity: 0.5 }]}
                 disabled={busy} onPress={save}>
        <Text style={s.btnText}>{busy ? "Saving…" : "Save"}</Text>
      </Pressable>
      {err ? <Note bad style={[s.mut, { color: C.bad }]}>{err}</Note> : null}
    </View>
  );
}

export default function Retire() {
  const { client } = useSession();
  const qc = useQueryClient();
  const { width } = useWindowDimensions();
  const [params, setParams] = useState<Record<string, string>>({});
  const [adjust, setAdjust] = useState(false);
  // "Retire at each age" opens on the ages that decide it (as on the web)
  const [everyAge, setEveryAge] = useState(false);
  const [form, setForm] =
    useState<Record<string, string> | null>(null);
  // the "saving not set — add it" box under the assumptions line
  const [askSaving, setAskSaving] = useState(false);
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
  // refetched on every visit: the Social Security editor below waits for a
  // read made since the screen opened, and a young cache would never get one
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client,
    refetchOnMount: "always" });
  const [openedAt] = useState(() => Date.now());
  const ssFresh = settings.dataUpdatedAt >= openedAt;
  const sp = useLocalSearchParams<{ wizard?: string }>();
  const [wizard, setWizard] =
    useState<boolean | null>(sp.wizard === "1" ? true : null);
  // The wizard's step saves and its exits share one chain: a mark sent
  // beside a step still in flight can be answered first, and the step's
  // reply (a whole document without the mark) would then seed the cache
  // last and reopen the walkthrough.
  const wizSaves = useRef<Promise<unknown>>(Promise.resolve());
  const markWizard = (v: "done" | "skipped") => {
    wizSaves.current = wizSaves.current
      .then(() => saveSettings(qc, client!, { ret_wizard_steps: { done: v } }))
      .catch(() => {});
  };
  const marks = settings.data?.ret_wizard_steps;
  // never for a viewer (the web's rule): the wizard's exits write the
  // mark, a viewer 403s — they see the page as it stands
  const viewer = useViewer();
  const needsWizard = wizard === null && !viewer
    && settings.data !== undefined
    // a walkthrough left halfway re-opens where it stopped: its first
    // answer saved the birthdate, which alone would read as "no need"
    && !marks?.done
    && (!settings.data.birthdate || retWizardMidway(marks))
    && Object.keys(params).length === 0;
  // the wizard opens on the saved progress, so it waits for settings; a
  // read that failed must not open it on generic defaults, whose step
  // saves would write over the stored answers
  if (wizard === true && !settings.data)
    return settings.isPending
      ? <Text style={[s.wrap, s.center]}>{SAVE_WAITS_LOADING}</Text>
      : (
        <View style={[s.wrap, s.center]}>
          <Text style={{ color: C.mut }}>{SAVE_WAITS_FAILED}</Text>
          <Text style={{ color: C.accent, marginTop: 8 }}
                onPress={() => settings.refetch()}>Try again</Text>
        </View>
      );
  if ((wizard === true || needsWizard) && query.data) {
    return (
      <RetireWizard
        splitSeed={!!query.data.inputs.saving_hint}
        defaults={{ ...query.data.inputs,
                    // older-server path: employer_mo stands in for the
                    // tax-deferred figure an older server has no td_mo for
                    employer_mo: query.data.inputs.td_mo
                      ?? query.data.inputs.employer_mo }}
        serverReadsAnswers={query.data.inputs.spend_source !== undefined}
        marks={marks}
        answers={settings.data?.ret_wizard_answers}
        // the mark seeds ["settings"] from the reply: needsWizard reads
        // it, and a stale copy would re-show the wizard on the next visit
        saves={wizSaves}
        onDone={(p) => {
          markWizard("done");
          // the answers are saved and the projection reads them; `p`
          // holds only answers that did not save, as what-ifs
          setParams(p); setWizard(false); setForm(null);
          qc.invalidateQueries({ queryKey: ["retirement"] });
        }}
        onSkip={() => {
          markWizard("skipped");
          setWizard(false);
        }} />
    );
  }
  const r = query.data ?? lastGood;
  const whatIfErr = query.isError && !query.data
    ? whatIfRefusal(query.error)
      ?? failLine(query.error, "the retirement plan", "phone").text
    : null;
  const inp = r?.inputs;
  const f = form ?? (inp ? {
    // age MUST ride the recalc — dropping it re-projects from the
    // server's derived age and silently discards a wizard answer
    age: String(inp.age),
    spend: inp.spend == null ? "" : String(inp.spend), ret: inp.ret.toFixed(1),
    infl: inp.infl.toFixed(1), end: String(inp.end),
    ssage: String(inp.ssage), stockpct: String(inp.stockpct),
    // older-server path: employer_mo stands in for the tax-deferred
    // figure an older server has no td_mo for
    employer_mo: String(inp.td_mo ?? inp.employer_mo),
    // an older server has no Roth figure: blank stays out of the query
    roth_mo: inp.roth_mo == null ? "" : String(inp.roth_mo),
    taxable_mo: String(inp.taxable_mo), resume: String(inp.resume),
    // blank = the server's default (cash keeps pace with inflation)
    cash: inp.cash == null ? "" : inp.cash.toFixed(1),
  } : null);
  const set = (k: string) => (v: string) =>
    setForm({ ...(f ?? {}), [k]: v });
  // the headline is the history replay (specs/retirement-figures.md); an
  // older server sends no verdict and its steady-model age stands alone
  const v = r?.verdict ?? null;
  const can = v ? v.age : (r?.earliest ?? null);
  // where the saving came from: an older server names only three sources
  const savingFrom = inp?.saving_basis ?? inp?.saving_source;
  const lastAge = r?.rows[r.rows.length - 1]?.age;
  const keyAges = keyRetireAges((r?.rows ?? []).map((row) => row.age), can,
                                r?.stress_ref_age);
  // the worst stress scenario's spend and the target's margin under it,
  // both the server's (absent from an older server: the tile is left out)
  const worst = r?.stress_floor_spend ?? null;
  const margin = r?.stress_floor_vs_spend ?? null;
  // no spend to plan for: nothing was projected, the screen asks for one
  const needed = !!r && (r.spend === null || r.spend_now === null
                         || r.stress_ref_age === null);
  // read only where `needed` is false, so both are the server's figures
  const target = r && !needed ? r.spend : null;
  const spendNow = r && !needed ? r.spend_now : null;
  const recalc = () => {
    if (!f || !inp) return;
    // a blank field is "use the default" and must not ride the query as
    // an empty value
    // the Roth box always rides where the server offers it (blank is $0):
    // a query without it is an older client's, whose employer figure
    // holds the ledger's Roth
    setParams(whatIfParams(inp.roth_mo != null && f.roth_mo.trim() === ""
                           ? { ...f, roth_mo: "0" } : f));
  };

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => {
                                        query.refetch();
                                        settings.refetch();
                                      }} />}>
      <StaleLine query={query} />
      {query.isPending && !r && <Skeleton />}
      {query.isError && !r && (
        <FailedLine q={query} what="the retirement plan" />
      )}
      {r && needed && (
        <Card>
          <Text style={s.mut}>Retirement plan</Text>
          <Text style={[s.big, { fontSize: 20 }]}>
            What do you plan to spend a year?</Text>
          <Text style={[s.mut, { marginTop: 4 }]}>
            Your ledger doesn&apos;t hold a year of spending to start from
            yet, so the plan needs the amount you expect to spend each year
            in retirement, in today&apos;s dollars. Nothing is projected
            until it has one.
          </Text>
          {viewer
            ? <Text style={s.mut}>The household&apos;s owner can add it.</Text>
            : <SpendAsk onWizard={() => setWizard(true)} />}
          {whatIfErr
            ? <Text style={[s.mut, { color: C.bad, marginTop: 6 }]}>
                {whatIfErr}
              </Text>
            : null}
        </Card>
      )}
      {r && !needed && inp && f && target !== null && (
        <>
          <Card>
            <Text style={s.mut}>
              Earliest retirement at {m$(target)}/yr
              {inp.spend_source ? ` · spend: ${SPEND_FROM[inp.spend_source]}` : ""}
            </Text>
            {can !== null ? (
              <Text style={s.big}>
                age {can}
                <Text style={{ color: C.good, fontSize: 13,
                               fontWeight: "400" }}>
                  {"  "}{v ? `lasts in ${v.in_ten} of 10 past markets — ` : "feasible — "}
                  money lasts to {inp.end}
                </Text>
              </Text>
            ) : (
              <Text style={[s.big, { color: C.bad }]}>
                not by {lastAge}
                <Text style={{ fontSize: 13, fontWeight: "400" }}>
                  {"  "}{v ? `lasts in ${v.in_ten} of 10 past markets at ${v.at_age}`
                    : "not sustainable without more saving"}
                </Text>
              </Text>
            )}
            {can === null && ((v ? v.catch_up : r.catch_up) ?? []).length > 0 ? (
              <Text style={[s.mut, { marginTop: 4 }]}>
                <Text style={{ color: C.text }}>
                  {v ? "To last in 9 of 10, save " : "To get back on track, save "}
                  {((v ? v.catch_up : r.catch_up) ?? []).map((c, k) => (
                    <Text key={c.age}>
                      {k > 0 ? ", or " : ""}
                      <Text style={{ fontWeight: "700" }}>{m$(c.extra_monthly)}/mo more</Text>
                      {" "}to retire at {c.age}
                    </Text>
                  ))}
                </Text>
                {inp.saving_mo
                  ? ` — starting now, on top of the ${m$(inp.saving_mo)}/mo already counted (${SAVING_FROM[savingFrom ?? "none"]}).`
                  : " — starting now."}
              </Text>
            ) : null}
            {v ? (
              <Text style={[s.mut, { marginTop: 4 }]}>
                Steady markets ({(r.real_return * 100).toFixed(1)}% a year after
                inflation, every year):{" "}
                {v.steady_age !== null ? `age ${v.steady_age}` : `not by ${lastAge}`}
                {v.steady_age === null && (r.catch_up ?? []).length > 0
                  ? ` — save ${(r.catch_up ?? []).map((c) =>
                      `${m$(c.extra_monthly)}/mo more to retire at ${c.age}`).join(" or ")}`
                  : ""}
              </Text>
            ) : null}
            {spendNow !== null ? (
              <KV k={`Retire today (${inp.age}) supports, steady markets`}
                  v={`${m$(spendNow)}/yr — after-tax, today's dollars`} />
            ) : null}
            <KV k="You have today"
                v={r.grow_to_67 != null
                  ? `${m$(r.buckets.total)} → ~${m$(r.grow_to_67)} by 67`
                  : m$(r.buckets.total)} />
            <Text style={s.mut}>
              accounts less card balances — property isn&apos;t counted
            </Text>
            {r.grow_to_67 != null ? (
              <Text style={s.mut}>
                at {(r.real_return * 100).toFixed(1)}%/yr real
                {!inp.saving_yr ? " (saving nothing)" : ""}
              </Text>
            ) : null}
            {worst !== null && margin !== null ? (<>
            <KV k={`Worst stress scenario, retiring at ${r.stress_ref_age}`}
                v={`${m$(worst)}/yr`}
                tone={margin >= 0 ? "good" : "bad"} />
            <Text style={s.mut}>
              {margin >= 0 ? "+" : "−"}{m$(Math.abs(margin))} vs target —
              the most you could spend and survive the worst stress scenario
            </Text>
            </>) : null}
            <Text style={[s.mut, { marginTop: 8 }]}>
              <Text style={{ color: C.text, fontWeight: "700" }}>
                Assumptions:{" "}
              </Text>
              {inp.ret}% return · {inp.infl}% inflation
              {inp.cash != null ? ` · cash ${inp.cash}%` : ""} · to {inp.end} ·
              SS at {inp.ssage}
              {inp.has_sched ? ` (${m$(inp.your_ss)}/mo)`
                : !viewer ? (
                  <Text>
                    {" ("}
                    <Text style={{ color: C.accent }}
                          onPress={() => setAdjust(true)}>no SSA statement</Text>
                    {")"}
                  </Text>
                ) : " (no SSA statement)"}
              {" · "}{inp.stockpct}% stocks ·{" "}
              {inp.saving_source === "none" ? (!viewer ? (
                  <Text style={{ color: C.accent }}
                        onPress={() => setAskSaving((a) => !a)}>
                    saving not set — add it</Text>
                ) : "saving not set")
                : <>{inp.saving_yr
                    ? `saving ${m$(inp.saving_yr)}/yr`
                      + (inp.resume > inp.age ? ` from ${inp.resume}` : "")
                    : "saving nothing"}
                  {savingFrom ? ` (${SAVING_FROM[savingFrom]})` : ""}</>}
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
                  {inp.roth_mo != null ? (
                    <Fld label="Roth $/mo" value={f.roth_mo}
                         onChange={set("roth_mo")} />
                  ) : null}
                  <Fld label="taxable $/mo" value={f.taxable_mo}
                       onChange={set("taxable_mo")} />
                  <Fld label="resume at" value={f.resume}
                       onChange={set("resume")} />
                </View>
                <Pressable accessibilityRole="button" style={[s.btn, { alignSelf: "flex-start" },
                                   query.isFetching && { opacity: 0.5 }]}
                           disabled={query.isFetching}
                           onPress={recalc}>
                  <Text style={s.btnText}>
                    {query.isFetching ? "Recalculating…" : "Recalculate"}
                  </Text>
                </Pressable>
              </View>
            )}
            {/* a saved copy may be older than another device's save, and
                saving these boxes replaces the schedule: they wait for a read
                made since the screen opened */}
            {adjust && !viewer && settings.data && ssFresh && (
              <SsEstimates saved={(settings.data.ss_estimates ?? null) as
                Record<string, number> | null} />
            )}
            {adjust && !viewer && !ssFresh && (
              <Text style={[s.mut, { marginTop: 8 }]}>
                {settings.isError
                  ? failLine(settings.error, "your Social Security estimates",
                             "phone").text
                  : "Loading your Social Security estimates…"}
              </Text>
            )}
            {/* each saving figure by its tax treatment, and an offer of
                the ledger's Roth saving a saved answer leaves out — the
                server's words; an older server sends neither */}
            {(inp.saving_parts ?? []).length > 0 ? (
              <Text style={[s.mut, { marginTop: 4 }]}>
                {(inp.saving_parts ?? []).map((p) => p.text).join(" · ")}
              </Text>
            ) : null}
            {inp.saving_hint ? (
              <Note style={[s.mut, { marginTop: 6 }]}>{inp.saving_hint}</Note>
            ) : null}
            {askSaving && !viewer && inp.saving_source === "none"
              ? <SavingAsk withRoth={inp.roth_mo != null} /> : null}
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
                  — {r.growth_path_retire_age != null
                    ? `retiring at ${r.growth_path_retire_age}, the middle of past markets, `
                    : ""}today&apos;s dollars
                </Text>
              </H>
              <Sparkline width={width - 24 - 28} data={r.growth_path}
                         subject="Projected portfolio in today's dollars" />
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
            {r.buckets.cards_from_cash ? (
              <Text style={s.mut}>
                after {m$(r.buckets.cards_from_cash)} of card balances
              </Text>
            ) : null}
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
              <KV k="The most you could spend and survive every year since 1928"
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
                <Text style={[s.num, x.under_target && { color: C.bad }]}>
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
              <Text style={[s.head, { width: 52 }]}>Past</Text>
              <Text style={[s.head, { width: 64 }]}>Steady</Text>
              <Text style={s.head}>Max spend</Text>
            </View>
            {(everyAge ? r.rows
                       : r.rows.filter((row) => keyAges.has(row.age)))
              .map((row) => (
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
                {/* one verdict a row, the headline's (web: Retirement.tsx):
                    past markets carry the colour, steady is plain text */}
                <Text style={[s.num, { width: 52 },
                  row.hist_lasts !== undefined
                    && { color: row.hist_lasts ? C.good : C.bad }]}>
                  {row.hist_pct !== null
                    ? `${row.hist_pct.toFixed(1)}%` : "·"}
                </Text>
                <Text style={{ width: 64, textAlign: "right",
                               fontSize: 12, color: C.mut }}>
                  {row.feasible ? "lasts"
                    : row.fail_age ? `out at ${row.fail_age}` : "no"}
                </Text>
                <Text style={[s.num, row.meets_target && { color: C.good }]}>
                  {m$(row.max_spend)}
                </Text>
              </View>
            ))}
            {keyAges.size < r.rows.length && (
              <Pressable accessibilityRole="button"
                         accessibilityState={{ expanded: everyAge }}
                         onPress={() => setEveryAge(!everyAge)}
                         style={{ paddingVertical: 8 }}>
                <Text style={{ color: C.accent, fontSize: 13 }}>
                  {everyAge ? "Show key ages only"
                    : `Show every age (${r.rows.length})`}
                </Text>
              </Pressable>
            )}
            <Text style={[s.mut, { marginTop: 6 }]}>
              Today&apos;s dollars, after tax, drawn cash → taxable →
              tax-deferred → Roth; Social Security from your claim age;
              RMDs from 72, 73 or 75 by birth year (IRS Uniform Lifetime
              Table), excess reinvested taxable. “Past” is the share of
              every return sequence since 1928 that lasted to {inp.end} —
              green from 9 in 10, which is how the earliest age is chosen.
              “Steady” is the same plan at {inp.ret}% a year every year;
              “Max spend” is the largest annual draw that just lasts to
              {" "}{inp.end} there.
            </Text>
          </Card>
        </>
      )}
    </ScrollView>
  );
}

// the 5-step walkthrough — each step's answers are saved as it is left,
// and /api/retirement reads them; onDone hands over only answers that did
// not save (a read-only login, a dropped connection) as what-ifs
function RetireWizard({ defaults, splitSeed, serverReadsAnswers, marks, answers, onDone, onSkip, saves }: {
  // the reply's saving figures are a saved total split by the ledger (it
  // carries saving_hint), not a what-if: the saving boxes open on them
  splitSeed?: boolean;
  // an older server projects only from the query, never from saved answers
  serverReadsAnswers?: boolean;
  // the inputs the projection used (null spend: none to plan for yet)
  defaults: Partial<Record<"age" | "spend" | "employer_mo" | "roth_mo"
                           | "taxable_mo" | "ret" | "infl" | "end" | "ssage",
                           number | null>>;
  // the saved progress: which steps are answered, and their answers
  marks?: Record<string, string | null> | null;
  answers?: Record<string, unknown> | null;
  onDone: (p: Record<string, string>) => void;
  onSkip: () => void;
  // the host's chain of settings saves: every step save goes on it, and
  // the host's exits put their done/skipped mark on it after them
  saves: MutableRefObject<Promise<unknown>>;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  // read once, at mount: the saves below seed settings as they land, and
  // re-reading them would move the boxes under the person's thumb
  // a saved answer, else the figure the projection used, else blank (the
  // server's default): the wizard keeps no defaults of its own
  const [seed] = useState(() => retWizardPrefill(answers, defaults,
                                                    !!splitSeed));
  const [i, setI] = useState(() => retWizardResumeAt(marks));
  // the step on screen, or none once the wizard has closed: a save's late
  // reply acts only if the person is still on the step that sent it
  const stepNow = useRef(i);
  useEffect(() => {
    stepNow.current = i;
    return () => { stepNow.current = -1; };
  }, [i]);
  const stillOn = (at: number) => stepNow.current === at;
  const [age, setAge] = useState(seed.age);
  const [birth, setBirth] = useState("");
  const [spend, setSpend] = useState(seed.spend);
  const [employer, setEmployer] = useState(seed.employer_mo);
  const [roth, setRoth] = useState(seed.roth_mo);
  const [taxable, setTaxable] = useState(seed.taxable_mo);
  const [ret, setRet] = useState(seed.ret);
  // an older server has no Roth figure: no box, and nothing sent for it
  const withRoth = defaults.roth_mo != null;
  const [infl, setInfl] = useState(seed.infl);
  const [end, setEnd] = useState(seed.end);
  const [ssage, setSsage] = useState(seed.ssage);
  // Step saves run one after another: each reply seeds ["settings"] with
  // the whole document, so an earlier reply landing last would put the
  // later step's mark back to missing. The chain is the host's, so the
  // done/skipped mark its exits write lands after every step's reply.
  const saveStep = (k: typeof RET_WIZARD_STEPS[number]) => {
    const body = retWizardStepBody(k, {
      age, spend, employer_mo: employer,
      ...(withRoth ? { roth_mo: roth } : {}), taxable_mo: taxable,
      ret, infl, end, ssage });
    const link = saves.current.then(() => saveSettings(qc, client!, body));
    // the chain carries on past a refused link, so one failure never stops
    // the saves after it; the step that made this one reads its refusal
    saves.current = link.catch(() => {});
    return link;
  };
  // A refusal the person can correct (the server names the answer and its
  // range) is shown on its step, which waits. Any other — a viewer's or the
  // demo login's read-only 403, a dropped connection — is nothing they can
  // fix here and never traps them in the walkthrough. The same rule on
  // every step, the birthdate's included, and on the web.
  const stepRefusal = (e: unknown): string | null =>
    e instanceof ApiError && (e.status === 400 || e.status === 422)
      ? errText(e) : null;
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // a step whose answers did not land (nothing the person could correct)
  const unsaved = useRef(false);
  // The birthdate is the answer every later visit derives the starting
  // age from, so a save the server refuses as invalid (a 400 with a
  // reason the user can correct) is shown and the step waits, rather than
  // moving on with the age silently left out. Any other
  // refusal — a viewer's or the demo login's read-only 403, a dropped
  // connection — is nothing the user can fix here, so it never traps them
  // in the walkthrough. Resolves true when the wizard may move on.
  const saveBirth = async (): Promise<boolean> => {
    const b = birthdateIso(birth);
    if ("error" in b) { setErr(b.error); return false; }
    if (!b.iso) return true;
    setBusy(true);
    try {
      await saveSettings(qc, client!, { birthdate: b.iso });
      qc.invalidateQueries({ queryKey: ["retirement"] });
      return true;
    } catch (e) {
      const why = stepRefusal(e);
      if (why) { setErr(why); return false; }
      return true;
    } finally { setBusy(false); }
  };
  const STEPS = ["How old are you?",
                 "What do you want to spend in retirement?",
                 "How much are you saving each month?",
                 "A few market assumptions", "You're set"];
  // A cleared box means "use the default", so it is left out of the query
  // rather than sent as an empty value — the adjust panel does the same.
  // A saved answer reaches the projection only on a server that reads them;
  // an older one is handed every box, as the walkthrough always did.
  const params = () => serverReadsAnswers && !unsaved.current ? {} : whatIfParams({
    age, spend, employer_mo: employer,
    roth_mo: withRoth ? (roth.trim() || "0") : "",
    taxable_mo: taxable, ret, infl, end, ssage });
  const next = async () => {
    setErr(null);
    const at = i;
    if (i === 0) {
      const a = Number(age);
      const [lo, hi] = RET_WIZARD_BOUNDS.age;
      if (!Number.isFinite(a) || a < lo || a > hi) {
        setErr(`Enter an age between ${lo} and ${hi}.`); return;
      }
      const b = birthdateIso(birth);
      if ("error" in b) { setErr(b.error); return; }
    }
    // "60,000" is a spend, not a typo, and "12,50" is twelve-fifty: judge
    // it by the server's separator rules, as the query will send it
    const spendN = spend.replace(/[$\s]/g, "") === "" ? 0
      : parseMoneyDraft(spend) ?? NaN;
    if (i === 1 && (!Number.isFinite(spendN) || spendN < 0)) {
      setErr("Enter a non-negative annual spend."); return;
    }
    // the claim age is a whole year in the server's range: say so here,
    // as the other boxes do, rather than show the server's refusal
    if (RET_WIZARD_STEPS[i] === "assumptions" && ssage.trim() !== "") {
      const c = Number(ssage);
      const [lo, hi] = RET_WIZARD_BOUNDS.ssage;
      if (!Number.isInteger(c) || c < lo || c > hi) {
        setErr(`Enter a claim age from ${lo} to ${hi}, in whole years.`);
        return;
      }
    }
    // the birthdate goes up with the first answer, so the starting age it
    // gives survives a reload; a refusal the person can fix stops here
    if (i === 0 && !(await saveBirth())) return;
    if (!stillOn(at)) return;
    // the step's answers land before it is left: a value the server
    // refuses is shown here, on the step it belongs to
    setBusy(true);
    try {
      await saveStep(RET_WIZARD_STEPS[i]);
      if (!stillOn(at)) return;
    } catch (e) {
      if (!stillOn(at)) return;
      const why = stepRefusal(e);
      if (why) { setErr(why); return; }
      unsaved.current = true;
    } finally { setBusy(false); }
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
          <TextInput {...fieldA11y("Current age")} style={s.input} keyboardType="number-pad"
                     placeholder="current age" placeholderTextColor={C.mut}
                     value={age} onChangeText={setAge} />
          <TextInput {...fieldA11y("Birthdate (optional, YYYY-MM-DD)")}
                     style={s.input} autoCapitalize="none"
                     placeholder="birthdate (optional, YYYY-MM-DD)"
                     keyboardType="numbers-and-punctuation"
                     placeholderTextColor={C.mut}
                     value={birth} onChangeText={setBirth} />
        </>)}
        {i === 1 && (<>
          <Text style={s.mut}>
            Annual spending in today&apos;s dollars — housing, food,
            travel, the whole lifestyle.
          </Text>
          <TextInput {...fieldA11y("Target spend $/year")} style={s.input} keyboardType="number-pad"
                     placeholder="target spend $/year"
                     placeholderTextColor={C.mut}
                     value={spend} onChangeText={setSpend} />
        </>)}
        {i === 2 && (<>
          <Text style={s.mut}>
            Split tax-deferred employer accounts (401k/403b), Roth accounts
            and taxable brokerage if you like.
          </Text>
          <TextInput {...fieldA11y("Employer retirement $/mo")}
                     style={s.input} keyboardType="number-pad"
                     placeholder="employer retirement $/mo"
                     placeholderTextColor={C.mut}
                     value={employer} onChangeText={setEmployer} />
          {withRoth ? (
            <TextInput {...fieldA11y("Roth retirement $/mo")}
                       style={s.input} keyboardType="number-pad"
                       placeholder="Roth retirement $/mo"
                       placeholderTextColor={C.mut}
                       value={roth} onChangeText={setRoth} />
          ) : null}
          <TextInput {...fieldA11y("Taxable saving $/mo")} style={s.input} keyboardType="number-pad"
                     placeholder="taxable saving $/mo"
                     placeholderTextColor={C.mut}
                     value={taxable} onChangeText={setTaxable} />
        </>)}
        {i === 3 && (<>
          <Text style={s.mut}>
            Sensible defaults — change them if you already have a view.
          </Text>
          <View style={{ flexDirection: "row", gap: 6 }}>
            <TextInput {...fieldA11y("Return %")} style={[s.input, { flex: 1 }]}
                       // the server takes -20 to 30: a minus key is needed
                       keyboardType="numbers-and-punctuation"
                       placeholder="return %" placeholderTextColor={C.mut}
                       value={ret} onChangeText={setRet} />
            <TextInput {...fieldA11y("Inflation %")} style={[s.input, { flex: 1 }]}
                       keyboardType="numbers-and-punctuation"
                       placeholder="inflation %"
                       placeholderTextColor={C.mut}
                       value={infl} onChangeText={setInfl} />
            <TextInput {...fieldA11y("To age")} style={[s.input, { flex: 1 }]}
                       keyboardType="number-pad"
                       placeholder="to age" placeholderTextColor={C.mut}
                       value={end} onChangeText={setEnd} />
          </View>
          <Text style={s.mut}>Social Security claim age</Text>
          <TextInput {...fieldA11y("Social Security claim age")}
                     style={s.input} keyboardType="number-pad"
                     placeholder="claim age (62–70)" placeholderTextColor={C.mut}
                     value={ssage} onChangeText={setSsage} />
        </>)}
        {i === 4 && (
          <Text style={{ color: C.text, fontSize: 14, lineHeight: 21 }}>
            Age <Text style={{ fontWeight: "700" }}>{age}</Text> · spend{" "}
            <Text style={{ fontWeight: "700" }}>
              ${numv(spend).toLocaleString()}/yr
            </Text> · saving{" "}
            <Text style={{ fontWeight: "700" }}>
              ${(numv(employer) + (withRoth ? numv(roth) : 0)
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
          <Note bad style={{ color: C.bad, fontSize: 12 }}>{err}</Note>
        ) : null}
        <View style={{ flexDirection: "row", gap: 8 }}>
          {i > 0 && i < 4 && (
            <Pressable accessibilityRole="button" style={[s.btn, { backgroundColor: C.hover },
                               busy && { opacity: 0.5 }]}
                       // not while the step's save is in flight
                       disabled={busy}
                       onPress={() => { setErr(null); setI(i - 1); }}>
              <Text style={[s.btnText, { color: C.mut }]}>← Back</Text>
            </Pressable>
          )}
          {i < 4 ? (
            <Pressable accessibilityRole="button" style={[s.btn, busy && { opacity: 0.5 }]}
                       disabled={busy} onPress={next}>
              <Text style={s.btnText}>
                {i === 3 ? "Finish →" : "Continue →"}
              </Text>
            </Pressable>
          ) : (
            <Pressable accessibilityRole="button" style={s.btn} onPress={() => onDone(params())}>
              <Text style={s.btnText}>Open my plan →</Text>
            </Pressable>
          )}
        </View>
        {i < 4 && (
          <Text style={{ color: C.mut, fontSize: 12 }}
                onPress={() => {
                  // Skip abandons the remaining questions, not a typed
                  // answer — a birthdate already entered goes on the
                  // save chain ahead of the skip mark, as finishing would
                  // have saved it. It leaves at once, a save in flight
                  // or not: no settings save has a timeout, and a stalled
                  // one must never hold the person in the walkthrough
                  setErr(null);
                  const b = birthdateIso(birth);
                  if (!("error" in b) && b.iso) {
                    const iso = b.iso;
                    saves.current = saves.current
                      .then(() => saveSettings(qc, client!, { birthdate: iso }))
                      .then(() => qc.invalidateQueries(
                        { queryKey: ["retirement"] }), () => {});
                  }
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
  input: { backgroundColor: C.bg, borderColor: C.field, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 13,
           paddingHorizontal: 8, paddingVertical: 6 },
  btn: { backgroundColor: C.btn, borderRadius: 8,
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
