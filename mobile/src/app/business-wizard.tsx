// Guided setup for a business entity — the web BusinessWizard, native:
// a few plain questions → the entity, its accounts, and a startup-cost
// scan, then the Business page takes over. The flat form asked for
// name, structure, state, EIN and dates all at once; every one of those
// drives something invisible from the form (structure → Schedule-C
// buckets, dates → §195/§248 window + compliance calendar), so asking
// them cold got shrugs and blanks.
//
// The accounts step is deliberately inside the wizard: whole-account
// assignment is what actually separates business money from personal.
// It is also the one step that MOVES REAL MONEY between the two sides,
// so it defaults to nothing selected, states plainly what assignment
// does, and is skippable.
//
// Step marks live in tenant config (biz_wizard_steps), the same key the
// web writes — start on one client, resume on the other. The entity is
// created once, at the end of step 2; ?resume= reopens at the accounts
// step with the entity in hand and never creates a second business.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useEffect, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card } from "../components/ui";
import type { Account, Entity } from "../lib/api";
import { patchList, saveSettings, bizKeys, personalKeys, invalidateAll } from "../lib/cache";
import { plaidHostedLink } from "../lib/plaid";
import { bizWizardResumeStep } from "../lib/pure";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";
import { useMe, useOwner } from "../lib/viewer";
import { errText } from "../lib/api";

// what an assignment or a flag scan can have moved: the entity's books
// on one side, the personal verdict / ledger / reports on the other.

const kindIs = (a: { kind: string }, ...types: string[]) =>
  types.some((t) => (a.kind || "").startsWith(t));

const STRUCTURES = [
  ["sole_prop", "Sole proprietorship"],
  ["single_member_llc", "Single-member LLC"],
  ["multi_member_llc", "Multi-member LLC"],
  ["s_corp", "S corporation"],
] as const;

const STEPS = [
  { key: "what", title: "What's the business?",
    blurb: "The structure decides which Schedule-C buckets and filing "
      + "deadlines apply — it's the answer that shapes everything else." },
  { key: "when", title: "Where and when did it start?",
    blurb: "State and dates drive the compliance calendar, and the start "
      + "date opens the startup-cost deduction window (§195/§248)." },
  { key: "accounts", title: "Which accounts hold its money?",
    blurb: "Everything in an assigned account is treated as business "
      + "money and leaves your personal budget and net worth." },
  { key: "startup", title: "Look for startup costs?",
    blurb: "Money spent before the business opened can often be "
      + "deducted." },
  { key: "done", title: "You're set", blurb: "" },
] as const;

export default function BusinessWizard() {
  const { client } = useSession();
  const router = useRouter();
  const qc = useQueryClient();
  // the wizard creates an entity and moves accounts — a viewer's answers
  // would 403 on the last step. Every entry point already hides it from
  // a viewer; the route itself (a deep link, restored navigation) must
  // too, as the web's standalone wizard and the retirement wizard do.
  // useViewer fails closed while /api/me loads, and a redirect is not a
  // hidden button — bouncing on the default would send every cold-start
  // owner away. Decide only once the role is known.
  const me = useMe();
  const viewer = me.data?.role === "viewer";
  // A member runs this wizard — creating the entity and assigning accounts
  // is editing chrome. The BANK DOOR is not: linking a connection is the
  // account owner's and the server refuses the rest, so the button that
  // opens it is drawn only for them. Adding the account by hand stays.
  const owner = useOwner();
  useEffect(() => {
    if (viewer) router.replace("/business" as never);
  }, [viewer, router]);
  const p = useLocalSearchParams<{ resume?: string; name?: string;
                                   started?: string }>();
  const resume = p.resume
    ? { id: String(p.resume), name: String(p.name ?? ""),
        started: p.started ? String(p.started) : "" }
    : null;

  const [i, setI] = useState(resume ? 2 : 0);
  const [err, setErr] = useState<string | null>(null);
  const [entityId, setEntityId] = useState<string | null>(
    resume?.id ?? null);
  const [name, setName] = useState(resume?.name ?? "");
  const [structure, setStructure] = useState("single_member_llc");
  const [usState, setUsState] = useState("");
  const [ein, setEin] = useState("");
  const [formation, setFormation] = useState("");
  const [started, setStarted] = useState(resume?.started ?? "");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [scanned, setScanned] = useState<number | null>(null);
  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState("checking");
  const [acctQ, setAcctQ] = useState("");
  const [showAll, setShowAll] = useState(false);

  const accounts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(), enabled: !!client });
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const marks = ((settings.data as
    { biz_wizard_steps?: Record<string, string> } | undefined)
    ?.biz_wizard_steps) || {};
  // the save returns the full settings view, so the marks land in the
  // cache from the reply — no second GET per step
  const mark = useMutation({
    mutationFn: (m: Record<string, string | null>) =>
      saveSettings(qc, client!, { biz_wizard_steps: m }),
  });
  // the resume point — see bizWizardResumeStep for why an interrupted
  // pair of question steps reopens at the first of them
  const firstOpen = bizWizardResumeStep(STEPS, marks);
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (touched || resume || settings.isPending) return;
    if (firstOpen > 0) setI(firstOpen);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [firstOpen, touched, settings.isPending]);
  const go = (n: number, key?: string) => {
    setTouched(true);
    if (key) mark.mutate({ [key]: "done" });
    setI(n);
  };
  const step = STEPS[i];
  const fail = (e: unknown) => setErr(errText(e));
  const finish = (id: string | null) => {
    if (id) router.replace("/business" as never);
    else router.back();
  };

  const create = useMutation({
    mutationFn: () => client!.entityCreate({
      name: name.trim(), structure,
      ...(usState.trim() ? { state: usState.trim() } : {}),
      ...(ein.trim() ? { ein: ein.trim() } : {}),
      ...(formation ? { formation_date: formation } : {}),
      ...(started ? { business_start_date: started } : {}),
    } as never),
    onSuccess: (e: Entity) => {
      setEntityId(e.id);
      // the new business joins the switcher before the summary returns
      qc.setQueryData<{ entities: Entity[] }>(["biz-summary"],
        (o) => o && { ...o, entities: [...o.entities, e] });
      qc.invalidateQueries({ queryKey: ["biz-summary"] });
      // the Business door is hidden until a business exists — refetch
      // `me` now or it stays hidden until the next cold start
      qc.invalidateQueries({ queryKey: ["me"] });
      setErr(null);
      setTouched(true);
      // both question steps land now: this is the first moment either
      // one's answers exist anywhere but this component
      mark.mutate({ what: "done", when: "done" });
      setI(2);
    },
    onError: fail,
  });
  // the assignments are independent writes — one round trip, not N
  const assign = useMutation({
    mutationFn: () => Promise.all([...picked].map(
      (id) => client!.accountAssignEntity(id, entityId))),
    onSuccess: () => {
      patchList<{ accounts: Account[] }, Account>(qc, ["accounts"],
        "accounts", (a) => picked.has(a.id), { entity_id: entityId });
      invalidateAll(qc, [["accounts"], ["biz-summary"],
        ...(entityId ? bizKeys(entityId) : []), ...personalKeys]);
      setErr(null);
      go(3, "accounts");
    },
    onError: fail,
  });
  // create + assign in one go: an unassigned manual account would sit
  // in the personal budget, the opposite of why it was made
  const addAcct = useMutation({
    mutationFn: async () => {
      const r = await client!.accountAdd(newName.trim(), newKind);
      await client!.accountAssignEntity(
        (r as { account_id: string }).account_id, entityId);
      return r;
    },
    onSuccess: () => { setNewName(""); setErr(null);
                       // the list is mounted, so invalidating refetches it
                       qc.invalidateQueries({ queryKey: ["accounts"] }); },
    onError: fail,
  });
  // the real bank first — a manual account is a container with no feed
  const connect = useMutation({
    mutationFn: () => plaidHostedLink(client!, ""),
    onSuccess: (r) => {
      setErr(r.kind === "add_failed" || r.kind === "update_failed"
        ? String(r.error ?? "link failed") : null);
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: fail,
  });
  const scan = useMutation({
    mutationFn: () => client!.entityImportFlags(entityId as string),
    onSuccess: (r) => {
      // the endpoint reports how many flagged transactions it moved
      setScanned(typeof r?.moved === "number" ? r.moved : 0);
      invalidateAll(qc, [...bizKeys(entityId as string), ["biz-summary"],
                         ["business"], ["transactions"]]);
      setErr(null);
      go(4, "startup");
    },
    onError: fail,
  });

  const busy = create.isPending || assign.isPending || scan.isPending
    || addAcct.isPending || connect.isPending;
  const allEligible = (accounts.data?.accounts ?? [])
    .filter((a) => kindIs(a, "depository", "credit"));
  const live = allEligible.filter(
    (a) => a.status !== "archived" && a.balance_current != null);
  const q = acctQ.trim().toLowerCase();
  const eligible = (showAll ? allEligible : live)
    .filter((a) => !q
      || `${a.name} ${a.institution_name || ""}`.toLowerCase()
        .includes(q))
    .sort((a, b) => `${a.institution_name || ""}${a.name}`
      .localeCompare(`${b.institution_name || ""}${b.name}`));

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => accounts.refetch()} />}>
      <StaleBanner query={settings} />
      <Card>
        <Text style={s.mut}>
          {resume ? `Continuing setup for ${resume.name || "your business"}`
                  : `Step ${i + 1} of ${STEPS.length}`}
        </Text>
        {/* passive progress dots — the flow is strict; these report
            position rather than offering navigation */}
        <View style={{ flexDirection: "row", gap: 6, marginVertical: 8 }}>
          {STEPS.map((st, n) => (
            <View key={st.key}
                  style={{ width: 10, height: 10, borderRadius: 5,
                           backgroundColor: n === i ? C.warn
                             : n < i ? C.good : C.border }} />
          ))}
        </View>
        <Text style={s.h1}>{step.title}</Text>
        {!!step.blurb && <Text style={s.mut}>{step.blurb}</Text>}
        {err && (
          <Text style={{ color: C.bad, fontSize: 12, marginTop: 4 }}>
            {err}
          </Text>
        )}

        {step.key === "what" && (
          <>
            <TextInput style={s.input} placeholder="Acme LLC"
                       placeholderTextColor={C.mut} value={name}
                       onChangeText={setName} />
            <View style={{ flexDirection: "row", flexWrap: "wrap",
                           gap: 6, marginTop: 6 }}>
              {STRUCTURES.map(([v, l]) => (
                <Pressable key={v}
                           style={[s.chip, structure === v && s.chipOn]}
                           onPress={() => setStructure(v)}>
                  <Text style={{ color: structure === v ? C.text : C.mut,
                                 fontSize: 12 }}>{l}</Text>
                </Pressable>
              ))}
            </View>
          </>
        )}

        {step.key === "when" && (
          <>
            <TextInput style={s.input} placeholder="State (optional) — CA"
                       placeholderTextColor={C.mut} autoCapitalize="characters"
                       value={usState} onChangeText={setUsState} />
            <TextInput style={s.input}
                       placeholder="EIN (optional, stored encrypted) — 12-3456789"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={ein} onChangeText={setEin} />
            <TextInput style={s.input}
                       placeholder="Formation date (optional) — YYYY-MM-DD"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={formation} onChangeText={setFormation} />
            <TextInput style={s.input}
                       placeholder="Business start date (optional) — YYYY-MM-DD"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={started} onChangeText={setStarted} />
          </>
        )}

        {step.key === "when" && !name.trim() && (
          <Text style={[s.mut, { color: C.warn }]}>
            This business still needs a name — go back to the first
            question and give it one.
          </Text>
        )}

        {step.key === "accounts" && (
          <>
            <Text style={[s.mut, { color: C.warn }]}>
              Assigning an account moves ALL of its transactions — past
              and future — out of your personal budget, cash flow and
              net worth. Only pick accounts the business actually owns.
              You can change this any time on the Accounts page.
            </Text>
            <TextInput style={s.input} placeholder="search accounts"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={acctQ} onChangeText={setAcctQ} />
            {allEligible.length !== live.length && (
              <Text style={{ color: C.accent, fontSize: 12, marginTop: 4 }}
                    onPress={() => setShowAll((x) => !x)}>
                {showAll
                  ? `hide ${allEligible.length - live.length} closed/archived`
                  : `show ${allEligible.length - live.length} closed/archived`}
              </Text>
            )}
            {eligible.map((a) => (
              <Pressable key={a.id}
                         style={{ flexDirection: "row", gap: 8,
                                  alignItems: "center",
                                  paddingVertical: 5 }}
                         onPress={() => setPicked((prev) => {
                           const next = new Set(prev);
                           if (next.has(a.id)) next.delete(a.id);
                           else next.add(a.id);
                           return next;
                         })}>
                <Text style={{ color: picked.has(a.id) ? C.accent : C.mut,
                               fontSize: 16 }}>
                  {picked.has(a.id) ? "☑" : "☐"}
                </Text>
                <Text style={{ color: C.text, fontSize: 13, flex: 1 }}
                      numberOfLines={1}>
                  {a.institution_name ? `${a.institution_name} — ` : ""}
                  {a.name}
                  <Text style={s.mut}>  {a.kind}</Text>
                </Text>
              </Pressable>
            ))}
            <Text style={[s.mut, { marginTop: 10, fontWeight: "700",
                                   color: C.text }]}>
              Business bank not connected yet?
            </Text>
            {owner ? (
              <>
                <Pressable style={[s.btn, busy && { opacity: 0.5 }]}
                           disabled={busy}
                           onPress={() => connect.mutate()}>
                  <Text style={s.btnText}>
                    {connect.isPending ? "waiting for your bank…"
                                       : "Connect an account"}
                  </Text>
                </Pressable>
                <Text style={s.mut}>
                  Opens your bank&apos;s sign-in. When it finishes, the
                  account appears in the list above — tick it and assign.
                </Text>
              </>
            ) : (
              <Text style={s.mut}>
                Linking a bank over this household is the account
                owner&apos;s — ask them, and the business account shows up
                in the list above for you to assign. You can still track it
                by hand below.
              </Text>
            )}
            <Text style={[s.mut, { marginTop: 8 }]}>
              Or track it by hand. A manual account holds a balance you
              maintain yourself — no transactions will flow into it.
              Use this only when the bank cannot be connected.
            </Text>
            <View style={{ flexDirection: "row", gap: 6,
                           alignItems: "center" }}>
              <TextInput style={[s.input, { flex: 1 }]}
                         placeholder="Acme LLC Checking"
                         placeholderTextColor={C.mut} value={newName}
                         onChangeText={setNewName} />
              {(["checking", "savings", "credit"] as const).map((k) => (
                <Pressable key={k}
                           style={[s.chip, newKind === k && s.chipOn]}
                           onPress={() => setNewKind(k)}>
                  <Text style={{ color: newKind === k ? C.text : C.mut,
                                 fontSize: 11 }}>{k}</Text>
                </Pressable>
              ))}
            </View>
            <Pressable style={[s.btn, (busy || !newName.trim())
                         && { opacity: 0.5 }]}
                       disabled={busy || !newName.trim()}
                       onPress={() => addAcct.mutate()}>
              <Text style={s.btnText}>
                {addAcct.isPending ? "adding…" : "Add account"}
              </Text>
            </Pressable>
          </>
        )}

        {step.key === "startup" && (
          <Text style={s.mut}>
            {started
              ? `Scan transactions before ${started} for costs that may `
                + "qualify as startup expenses. Nothing is deducted "
                + "automatically — matches are flagged for you to review."
              : "You didn't set a business start date, so there's no "
                + "window to scan. You can add one later and run this "
                + "from the Business page."}
          </Text>
        )}

        {step.key === "done" && (
          <Text style={s.mut}>
            {name || "Your business"} is set up
            {picked.size ? ` with ${picked.size} account${
              picked.size > 1 ? "s" : ""}` : ""}
            {scanned ? `, and ${scanned} possible startup cost${
              scanned > 1 ? "s" : ""} flagged` : ""}.
            Everything is editable on the Business page.
          </Text>
        )}

        <View style={{ flexDirection: "row", gap: 8, marginTop: 12,
                       flexWrap: "wrap", alignItems: "center" }}>
          {step.key === "what" && (
            <Pressable style={[s.btn, !name.trim() && { opacity: 0.5 }]}
                       disabled={!name.trim()}
                       onPress={() => { setErr(null); go(1); }}>
              <Text style={s.btnText}>Continue</Text>
            </Pressable>
          )}
          {step.key === "when" && (
            <>
              {/* the name is collected a step earlier and is required by
                  the server, so a blank one must not be offerable here */}
              <Pressable style={[s.btn, (busy || !name.trim())
                           && { opacity: 0.5 }]}
                         disabled={busy || !name.trim()}
                         onPress={() => create.mutate()}>
                <Text style={s.btnText}>
                  {create.isPending ? "creating…" : "Create business"}
                </Text>
              </Pressable>
              {/* the only step with a way back. Everything after this one
                  has already created the entity, so re-entering the
                  questions would offer to create a second business. */}
              <Text style={{ color: C.mut, fontSize: 13, padding: 6 }}
                    onPress={() => { if (busy) return;
                      setErr(null); setTouched(true); setI(0); }}>
                Back
              </Text>
            </>
          )}
          {step.key === "accounts" && (
            <>
              <Pressable style={[s.btn, (busy || picked.size === 0)
                           && { opacity: 0.5 }]}
                         disabled={busy || picked.size === 0}
                         onPress={() => assign.mutate()}>
                <Text style={s.btnText}>
                  {assign.isPending ? "assigning…"
                    : `Assign ${picked.size || ""} account${
                        picked.size === 1 ? "" : "s"}`}
                </Text>
              </Pressable>
              <Text style={{ color: C.mut, fontSize: 13, padding: 6 }}
                    onPress={() => { if (busy) return;
                      setErr(null); setTouched(true);
                      mark.mutate({ accounts: "skipped" }); setI(3); }}>
                Skip for now
              </Text>
            </>
          )}
          {step.key === "startup" && (
            <>
              {!!started && (
                <Pressable style={[s.btn, busy && { opacity: 0.5 }]}
                           disabled={busy}
                           onPress={() => scan.mutate()}>
                  <Text style={s.btnText}>
                    {scan.isPending ? "scanning…"
                                    : "Scan for startup costs"}
                  </Text>
                </Pressable>
              )}
              <Text style={{ color: C.mut, fontSize: 13, padding: 6 }}
                    onPress={() => { if (busy) return;
                      setErr(null); setTouched(true);
                      mark.mutate(
                        { startup: started ? "skipped" : "done" });
                      setI(4); }}>
                {started ? "Skip" : "Continue"}
              </Text>
            </>
          )}
          {step.key === "done" && (
            <Pressable style={s.btn}
                       onPress={() => { mark.mutate({ finish: "done" });
                                        finish(entityId); }}>
              <Text style={s.btnText}>Open the Business page</Text>
            </Pressable>
          )}
          {step.key !== "done" && (
            <Text style={{ color: C.mut, fontSize: 13, padding: 6,
                           marginLeft: "auto" }}
                  onPress={() => !busy && finish(entityId)}>
              {entityId ? "Finish later" : "Cancel"}
            </Text>
          )}
        </View>
      </Card>
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  h1: { color: C.text, fontSize: 17, fontWeight: "700", marginTop: 2 },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17, marginTop: 4 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8, marginTop: 6 },
  chip: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 14, paddingHorizontal: 10, paddingVertical: 4 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8, marginTop: 8,
         alignSelf: "flex-start" },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});
