// Per-payee recurring history — the web BillsHistory page, native: bill
// status header, ledger fit ("edit with these values", hint dismiss/
// restore), add-as-bill with ✨ suggest-from-history, merchant-wide
// recategorize (preview → confirm-with-count → apply → undo),
// payee-scoped proposals, lifetime totals, the 24-month ledger, monthly
// totals.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useMemo, useState } from "react";
import { Pressable, ScrollView, StyleSheet, Text, TextInput,
         View } from "react-native";

import StaleBanner from "../components/stale-banner";
import Sheet from "../components/sheet";
import TxnRow from "../components/txn-row";
import { Card, H, KV, Pill } from "../components/ui";
import { errText, setHandedOffTxn, type Bill, type BillsData, type BillsHistoryData,
         type Proposal } from "../lib/api";
import { patchDismissedHint, patchList, patchQuery,
         removeFromList } from "../lib/cache";
import { useSession } from "../lib/session";
import { useDemo, useViewer } from "../lib/viewer";
import { C, mmddyy, money } from "../lib/theme";
import MerchantAvatar from "../components/merchant-avatar";
import { catLabel, filterCategories } from "../lib/pure";

// the web page's health pill wording
const HEALTH_LABEL: Record<string, { text: string;
                                     tone?: "good" | "warn" | "bad" }> = {
  ok: { text: "matching", tone: "good" },
  drifting: { text: "drifting", tone: "warn" },
  stale: { text: "stale", tone: "bad" },
  mismatch: { text: "amounts differ", tone: "warn" },
  misdated: { text: "misdated", tone: "warn" },
};
// any unknown health falls through to off-ledger, like the web's hpill
const healthPill = (h: string) =>
  HEALTH_LABEL[h] ?? { text: "off-ledger" as const, tone: undefined };

const SIMPLE_CADENCES: [string, string][] = [
  ["DAILY:1", "daily"], ["WEEKLY:1", "weekly"], ["MONTHLY:1", "monthly"],
  ["YEARLY:1", "yearly"], ["ENVELOPE:1", "envelope (monthly pool)"],
  ["ENVELOPE:12", "envelope (annual pool)"],
];

export default function BillHistory() {
  const { payee } = useLocalSearchParams<{ payee: string }>();
  const { client } = useSession();
  const router = useRouter();
  const viewer = useViewer();
  // the demo instance refuses merchant-wide category writes (its login is
  // shared) — the app hides the card there rather than offering a picker
  // that 403s after the confirm
  const demo = useDemo();
  const qc = useQueryClient();
  // ledger rows render in pages (a category-pool envelope is hundreds)
  const [shownTxns, setShownTxns] = useState(60);
  const q = useQuery({
    queryKey: ["payee-history", payee],
    queryFn: () => client!.billsHistory(String(payee)),
    enabled: !!client && !!payee,
  });
  const refetch = () => {
    qc.invalidateQueries({ queryKey: ["payee-history", payee] });
    qc.invalidateQueries({ queryKey: ["bills"] });
  };
  // a new bill reaches the calendar and Today's numbers
  const afterBillWrite = () => {
    refetch();
    qc.invalidateQueries({ queryKey: ["calendar"] });
    qc.invalidateQueries({ queryKey: ["today"] });
  };
  // a merchant-wide recategorize (and its undo) moves rows in the ledger,
  // the verdict and the month/year lenses, and the Merchants detail sheet
  // shows the category + rule it writes
  const afterRecategorize = () => {
    refetch();
    qc.invalidateQueries({ queryKey: ["merchant-detail"] });
    qc.invalidateQueries({ queryKey: ["transactions"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["lens"] });
  };
  const hint = useMutation({
    mutationFn: (action: "dismiss" | "restore") =>
      client!.billsHint(String(payee), action),
    onSuccess: (r) => {
      // Dismiss ↔ Restore swap the moment the reply lands, here, on the
      // Bills row, and (via the settings patch) Budget's tweak count —
      // both already patched, so only ["bills"] needs a confirming
      // refetch (this screen's own query was just patched above)
      patchQuery<BillsHistoryData>(qc, ["payee-history", payee],
        { hint_dismissed: r.dismissed });
      patchList<BillsData, Bill>(qc, ["bills"], "bills",
        (b) => b.payee === payee, { hint_dismissed: r.dismissed });
      patchDismissedHint(qc, String(payee), r.dismissed);
      qc.invalidateQueries({ queryKey: ["bills"] });
    },
  });
  // a decided proposal leaves both lists now (the server has committed
  // it); approving writes a bill, rejecting writes none — only the list
  // and the budgets prefill (which reads pending income) can move then
  const proposal = useMutation({
    mutationFn: ({ pid, action }:
        { pid: string; action: "approve" | "reject" }) =>
      client!.billsProposal(pid, action),
    onSuccess: (_r, v) => {
      removeFromList<BillsHistoryData, Proposal>(qc, ["payee-history", payee],
        "proposals", (p) => p.id === v.pid);
      removeFromList<BillsData, Proposal>(qc, ["bills"], "proposals",
        (p) => p.id === v.pid);
      if (v.action === "approve") afterBillWrite(); else refetch();
      qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    },
  });
  const d = q.data;
  const bill = d?.bill;
  const fit = d?.fit;
  const cadLabel = (v: string) =>
    d?.cadences.find(([cv]) => cv === v)?.[1] ?? v;
  const health = d?.health ? healthPill(d.health.health) : null;
  const showDismiss = !!d?.health
    && (["mismatch", "stale", "misdated"].includes(d.health.health)
        || !!d.health.fit_diverges);

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}>
      <StaleBanner query={q} />
      {q.isPending && <Text style={s.center}>Loading…</Text>}
      {q.isError && !d && (
        <Text style={[s.center, { color: C.bad }]}
              onPress={() => q.refetch()}>
          Couldn't load this history — tap to retry.
        </Text>
      )}
      {d && (
        <>
          <Card>
            <View style={{ flexDirection: "row", alignItems: "center",
                           gap: 6, flexWrap: "wrap" }}>
              <MerchantAvatar name={d.payee} logo={d.logo} size={26} />
              <H>{d.payee}</H>
              {health && <Pill text={health.text} tone={health.tone} />}
              {bill && <Pill text={bill.bill_type} />}
              {bill?.income && <Pill text="income" tone="good" />}
            </View>
            <Text style={s.mut}>
              {bill ? (
                <>
                  Configured: {money(bill.amount)} {cadLabel(bill.cadence)}
                  {bill.due_on ? ` · next due ${mmddyy(bill.due_on)}` : ""}
                  {" · source "}{bill.source}
                </>
              ) : d.covered_by ? (
                <>
                  Already tracked by bill{" "}
                  <Text style={{ color: C.accent }}
                        onPress={() => router.push({
                          pathname: "/bill-history",
                          params: { payee: d.covered_by! } } as never)}>
                    {d.covered_by}
                  </Text>.
                </>
              ) : (
                "Not an active bill."
              )}
              {d.health?.last_match
                ? ` · last matched ${mmddyy(d.health.last_match)}${
                    d.health.matched != null
                      ? ` (${d.health.matched} matches)` : ""}`
                : ""}
            </Text>
            {fit && (
              <Text style={[s.mut, { marginTop: 4 }]}>
                <Text style={{ color: C.text, fontWeight: "700" }}>
                  Ledger says:{" "}
                </Text>
                {fit.label}
              </Text>
            )}
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8,
                           marginTop: 8 }}>
              {bill && !viewer && (
                <Pressable style={s.btn}
                           onPress={() => router.push({ pathname: "/bill",
                             params: { payee: d.payee } } as never)}>
                  <Text style={s.btnText}>Edit bill</Text>
                </Pressable>
              )}
              {bill && !viewer && fit && (
                <Pressable style={[s.btn, s.btnQuiet]}
                           onPress={() => router.push({ pathname: "/bill",
                             params: { payee: d.payee,
                                       pamt: fit.amount.toFixed(2),
                                       pcad: fit.cadence,
                                       pdue: fit.next_due ?? "" } } as never)}>
                  <Text style={[s.btnText, { color: C.accent }]}>
                    Edit with these values
                  </Text>
                </Pressable>
              )}
              {bill && !viewer && fit && d.hint_dismissed ? (
                <Pressable style={[s.btn, s.btnQuiet]}
                           disabled={hint.isPending}
                           onPress={() => hint.mutate("restore")}>
                  <Text style={[s.btnText, { color: C.mut }]}>
                    Restore hint
                  </Text>
                </Pressable>
              ) : bill && !viewer && fit && showDismiss ? (
                <Pressable style={[s.btn, s.btnQuiet]}
                           disabled={hint.isPending}
                           onPress={() => hint.mutate("dismiss")}>
                  <Text style={[s.btnText, { color: C.mut }]}>
                    Dismiss hint
                  </Text>
                </Pressable>
              ) : null}
            </View>
            {!bill && !d.covered_by && !viewer && (
              <AddAsBill payee={d.payee} fit={fit ?? null}
                         cadences={d.cadences} onDone={afterBillWrite} />
            )}
          </Card>

          {!viewer && !demo && (
            <MerchantCategory payee={d.payee} onDone={afterRecategorize} />
          )}

          {d.proposals.length > 0 && (
            <Card>
              <H>Pending proposals</H>
              {d.proposals.map((p) => {
                // this row's own decision in flight — never the whole list
                const busy = proposal.isPending
                  && proposal.variables?.pid === p.id;
                return (
                <View key={p.id} style={s.propRow}>
                  <View style={{ flex: 1 }}>
                    <View style={{ flexDirection: "row",
                                   alignItems: "center", gap: 6 }}>
                      <Pill text={catLabel(p.kind)}
                            tone={p.kind === "remove" ? "bad"
                              : p.kind === "add" ? "good" : "warn"} />
                      {p.bill_type === "envelope" && <Pill text="envelope" />}
                      <Text style={{ color: C.text, fontWeight: "700",
                                     fontSize: 14 }}>
                        {money(p.amount)}
                      </Text>
                    </View>
                    <Text style={s.mut}>{p.summary}</Text>
                  </View>
                  <Pressable style={[s.btn, busy && { opacity: 0.5 }]}
                             disabled={busy}
                             onPress={() => proposal.mutate({
                               pid: p.id, action: "approve" })}>
                    <Text style={s.btnText}>Approve</Text>
                  </Pressable>
                  <Pressable style={[s.btn, s.btnQuiet,
                                     busy && { opacity: 0.5 }]}
                             disabled={busy}
                             onPress={() => proposal.mutate({
                               pid: p.id, action: "reject" })}>
                    <Text style={[s.btnText, { color: C.mut }]}>Reject</Text>
                  </Pressable>
                </View>
                );
              })}
              {proposal.isError ? (
                <Text style={{ color: C.bad, fontSize: 12 }}>
                  {String(proposal.error)}
                </Text>
              ) : null}
            </Card>
          )}

          {d.lifetime.count > 0 && (
            <Card>
              <H>Lifetime total</H>
              <Text style={{ marginTop: 2 }}>
                <Text style={{ color: d.inflow ? C.good : C.text,
                               fontSize: 24, fontWeight: "700" }}>
                  {money(d.lifetime.total, false)}
                </Text>
                <Text style={s.mut}>
                  {"  "}{d.inflow ? "received" : "spent"} all-time ·{" "}
                  {d.lifetime.count} charge
                  {d.lifetime.count === 1 ? "" : "s"}
                </Text>
              </Text>
              {d.lifetime.active_months > 0 && (
                <KV k="Avg / active month"
                    v={`${money(d.lifetime.avg_monthly_active, false)} · ${
                        d.lifetime.active_months} active month${
                        d.lifetime.active_months === 1 ? "" : "s"}`}
                    tone="mut" />
              )}
              {d.lifetime.first && d.lifetime.last && (
                <KV k="First / last"
                    v={`${mmddyy(d.lifetime.first)} – ${
                        mmddyy(d.lifetime.last)}`} tone="mut" />
              )}
            </Card>
          )}

          <Card>
            <H>
              Transactions{" "}
              <Text style={[s.mut, { fontWeight: "400" }]}>(24 months)</Text>
            </H>
            {d.txns.length === 0 && (
              <Text style={s.mut}>
                No ledger transactions match this payee.
              </Text>
            )}
            {/* the server returns EVERY match over 24 months and this
                ScrollView mounts each row synchronously — render in
                pages so a category-pool envelope's hundreds of rows
                stay reachable without mounting them all at once */}
            {d.txns.slice(0, shownTxns).map((t) => (
              <TxnRow key={t.txn_id ?? t.id} t={{ ...t, id: t.txn_id ?? t.id }}
                      onPress={(x) => { setHandedOffTxn(x);
                        router.push({ pathname: "/txn",
                                      params: { id: x.id } }); }} />
            ))}
            {d.txns.length > shownTxns && (
              <Pressable onPress={() =>
                  setShownTxns((n) => n + 120)}>
                <Text style={{ color: C.accent, fontSize: 13,
                               textAlign: "center", marginTop: 6 }}>
                  show more — {d.txns.length - shownTxns} older rows
                </Text>
              </Pressable>
            )}
          </Card>

          {d.monthly.length > 0 && (
            <Card>
              <H>Monthly totals</H>
              {d.monthly.map((m) => (
                <KV key={m.month} k={mmddyy(m.month + "-01").slice(0, 5)}
                    v={`${money(m.total, false)} · ${m.count}`} tone="mut" />
              ))}
            </Card>
          )}
        </>
      )}
    </ScrollView>
  );
}

function AddAsBill({ payee, fit, cadences, onDone }: {
  payee: string;
  fit: { kind: string; amount: number; cadence: string;
         next_due: string; label: string } | null;
  cadences: [string, string][];
  onDone: () => void;
}) {
  const { client } = useSession();
  const [amount, setAmount] = useState(fit ? fit.amount.toFixed(2) : "");
  const [cadence, setCadence] = useState(fit?.cadence ?? "MONTHLY:1");
  const [nextDue, setNextDue] = useState(fit?.next_due ?? "");
  const [customN, setCustomN] = useState("2");
  const [customUnit, setCustomUnit] = useState<"MONTHLY" | "YEARLY">("YEARLY");
  const [suggested, setSuggested] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const effCadence = cadence === "CUSTOM"
    ? `${customUnit}:${Math.max(1, parseInt(customN, 10) || 1)}`
    : cadence;
  const add = useMutation({
    mutationFn: () => client!.billsSave({
      payee, amount, cadence: effCadence, next_due: nextDue || undefined }),
    onSuccess: onDone,
    onError: (e) => setErr(errText(e)),
  });
  // a ledger suggestion may be an unusual cadence not in the short list —
  // keep it selectable/visible rather than silently mis-showing
  const options: [string, string][] = [
    ...SIMPLE_CADENCES,
    ...(fit && !SIMPLE_CADENCES.some(([v]) => v === fit.cadence)
      ? [[fit.cadence,
          cadences.find(([v]) => v === fit.cadence)?.[1] ?? fit.cadence] as
          [string, string]]
      : []),
    ["CUSTOM", "custom interval…"],
  ];
  return (
    <View style={{ marginTop: 10, gap: 8 }}>
      <Text style={{ color: C.text, fontSize: 14, fontWeight: "700" }}>
        Add as recurring bill
      </Text>
      <TextInput style={s.input} placeholder="amount"
                 placeholderTextColor={C.mut} value={amount}
                 onChangeText={setAmount} keyboardType="decimal-pad" />
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
        {options.map(([v, l]) => (
          <Pressable key={v} style={[s.chip, cadence === v && s.chipOn]}
                     onPress={() => setCadence(v)}>
            <Text style={{ color: cadence === v ? C.text : C.mut,
                           fontSize: 12 }}>{l}</Text>
          </Pressable>
        ))}
      </View>
      {cadence === "CUSTOM" && (
        <View style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
          <Text style={s.mut}>every</Text>
          <TextInput style={[s.input, { minWidth: 52, flex: 0 }]}
                     keyboardType="number-pad" value={customN}
                     onChangeText={setCustomN} />
          {(["MONTHLY", "YEARLY"] as const).map((u) => (
            <Pressable key={u} style={[s.chip, customUnit === u && s.chipOn]}
                       onPress={() => setCustomUnit(u)}>
              <Text style={{ color: customUnit === u ? C.text : C.mut,
                             fontSize: 12 }}>
                {u === "MONTHLY" ? "month(s)" : "year(s)"}
              </Text>
            </Pressable>
          ))}
        </View>
      )}
      <TextInput style={s.input}
                 placeholder="next due (YYYY-MM-DD, optional)"
                 placeholderTextColor={C.mut} value={nextDue}
                 onChangeText={setNextDue} autoCapitalize="none" />
      <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap" }}>
        <Pressable style={[s.btn, (!amount || add.isPending)
                     && { opacity: 0.5 }]}
                   disabled={!amount || add.isPending}
                   onPress={() => add.mutate()}>
          <Text style={s.btnText}>Add</Text>
        </Pressable>
        {fit ? (
          <Pressable style={[s.btn, s.btnQuiet]}
                     onPress={() => { setAmount(fit.amount.toFixed(2));
                                      setCadence(fit.cadence);
                                      setNextDue(fit.next_due ?? "");
                                      setSuggested(true); }}>
            <Text style={[s.btnText, { color: C.accent }]}>
              ✨ Suggest from history
            </Text>
          </Pressable>
        ) : (
          // the fit needs ≥3 charges and a provable cycle — be honest
          // about why there's no button
          <Text style={[s.mut, { alignSelf: "center", flexShrink: 1 }]}>
            ✨ no suggestion — not enough charge history yet
          </Text>
        )}
      </View>
      {suggested && fit && (
        <Text style={s.mut}>
          Ledger says: {fit.label}
          {fit.kind === "envelope"
            ? " — saves as an envelope (monthly pool) bill" : ""}
        </Text>
      )}
      {err ? (
        <Text style={{ color: C.bad, fontSize: 12 }}
              onPress={() => setErr(null)}>{err}</Text>
      ) : null}
    </View>
  );
}

// bulk category override for the whole (canonical) merchant — the count
// is the TRUE merchant-wide scope, so the user consents before the write
function MerchantCategory({ payee, onDone }: {
  payee: string; onDone: () => void;
}) {
  const { client } = useSession();
  const [picking, setPicking] = useState(false);
  const [filter, setFilter] = useState("");
  const [cat, setCat] = useState("");
  const [confirm, setConfirm] = useState<{
    merchant: string; count: number; variants?: string[];
    flow_rows?: number; skipped_override?: number } | null>(null);
  const [undo, setUndo] = useState<{ count: number; merchant: string;
                                     undo: unknown } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const cats = useQuery({
    queryKey: ["categories"],
    queryFn: () => client!.categories(),
    enabled: !!client && picking,
  });
  // no flow categories: a merchant rule cannot make a merchant a
  // transfer — the server refuses that write — so listing them would only
  // produce an error after the confirm (the web picker leaves them out
  // too). Tenant categories that ARE flow names are filtered too.
  // re-filtered on every keystroke of `filter` — memoized so a tenant
  // with hundreds of categories doesn't re-run the dedupe+filter chain
  // (and re-render the whole unvirtualized picker list) per character
  const catList = useMemo(() => {
    const flow = new Set(cats.data?.plaid_flow ?? []);
    // filterCategories matches the typed text against each option's
    // DISPLAY label, which is what the rows below render — matching the
    // raw constant instead, typing "general services" exactly as shown
    // would find nothing
    return filterCategories([...new Set([
      ...(cats.data?.plaid_spend ?? []),
      ...(cats.data?.categories ?? []),
    ])].filter((c) => !flow.has(c)), filter);
  }, [cats.data, filter]);

  const preview = async (c: string) => {
    setErr(null); setBusy(true); setCat(c); setPicking(false);
    try { setConfirm(await client!.merchantCategoryPreview(payee, c)); }
    catch (e) { setErr(errText(e)); }
    finally { setBusy(false); }
  };
  const apply = async () => {
    setErr(null); setBusy(true);
    try {
      const r = await client!.merchantCategorySet(payee, cat);
      setUndo({ count: r.count, merchant: r.merchant, undo: r.undo });
      setConfirm(null); setCat(""); onDone();
    } catch (e) { setErr(errText(e)); }
    finally { setBusy(false); }
  };
  const doUndo = async () => {
    if (!undo) return;
    setBusy(true);
    try { await client!.merchantCategoryUndo(undo.undo);
          setUndo(null); onDone(); }
    catch (e) { setErr(errText(e)); }
    finally { setBusy(false); }
  };

  return (
    <Card>
      <H>Categorize this merchant</H>
      <Text style={s.mut}>
        Set one category for every past and future transaction of this
        merchant.
      </Text>
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 8 },
                   busy && { opacity: 0.5 }]}
                 disabled={busy}
                 onPress={() => setPicking(true)}>
        <Text style={s.btnText}>Apply to whole merchant…</Text>
      </Pressable>
      {confirm && (
        <View style={{ marginTop: 8, gap: 6 }}>
          <Text style={{ color: C.text, fontSize: 14 }}>
            {confirm.count > 0 ? (
              <>
                Move{" "}
                <Text style={{ fontWeight: "700" }}>{confirm.count}</Text>
                {" past transaction"}{confirm.count === 1 ? "" : "s"} to{" "}
                <Text style={{ fontWeight: "700" }}>
                  {catLabel(cat)}
                </Text>
              </>
            ) : (
              <>
                Every matched charge is already{" "}
                <Text style={{ fontWeight: "700" }}>
                  {catLabel(cat)}
                </Text>
              </>
            )}
            {" — and keep future charges there."}
          </Text>
          {/* what this write does to bank-labelled transfers — without
              this line "apply to all" would look broken */}
          {!!confirm.flow_rows && (
            <Text style={s.mut}>
              {confirm.flow_rows} of these row
              {confirm.flow_rows === 1 ? " is" : "s are"} labelled by your
              bank as a transfer, income or loan payment and WILL move too —
              your merchant rule outranks that label. They start counting as
              spending.
            </Text>
          )}
          {!!confirm.skipped_override && (
            <Text style={s.mut}>
              {confirm.skipped_override} row
              {confirm.skipped_override === 1 ? " has" : "s have"} a
              category you set by hand and will be left alone.
            </Text>
          )}
          {(confirm.variants?.length ?? 0) > 1 && (
            <Text style={s.mut}>
              Covers every name this page matches:{" "}
              {confirm.variants!.slice(0, 6).join(" · ")}
              {confirm.variants!.length > 6
                ? ` · +${confirm.variants!.length - 6} more` : ""}
            </Text>
          )}
          <View style={{ flexDirection: "row", gap: 8 }}>
            <Pressable style={s.btn} disabled={busy} onPress={apply}>
              <Text style={s.btnText}>Yes, recategorize</Text>
            </Pressable>
            <Pressable style={[s.btn, s.btnQuiet]} disabled={busy}
                       onPress={() => setConfirm(null)}>
              <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
            </Pressable>
          </View>
        </View>
      )}
      {undo && (
        <Text style={[s.mut, { marginTop: 8 }]}>
          Recategorized {undo.count} transaction
          {undo.count === 1 ? "" : "s"} for {undo.merchant}.{"  "}
          {/* guarded like Apply above it — unguarded, a double tap on a
              slow connection would send the undo twice against rows
              already reverted by the first */}
          <Text style={[{ color: C.accent }, busy && { opacity: 0.5 }]}
                onPress={() => !busy && doUndo()}>Undo</Text>
        </Text>
      )}
      {err ? (
        <Text style={{ color: C.bad, fontSize: 12, marginTop: 6 }}
              onPress={() => setErr(null)}>{err}</Text>
      ) : null}

      <Sheet visible={picking} onClose={() => setPicking(false)}
             title={`Category for ${payee}`}
             footer={
               <Pressable style={[s.btn, s.btnQuiet, { alignSelf: "center",
                                                       marginTop: 10 }]}
                          onPress={() => setPicking(false)}>
                 <Text style={[s.btnText, { color: C.mut }]}>Close</Text>
               </Pressable>}>
            <TextInput style={s.input} placeholder="filter…"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={filter} onChangeText={setFilter} />
            <ScrollView style={{ maxHeight: 420 }}>
              {cats.isPending && <Text style={s.mut}>Loading…</Text>}
              {catList.map((c) => (
                <Pressable key={c} style={s.catRow}
                           onPress={() => preview(c)}>
                  <Text style={{ color: C.text, fontSize: 15 }}>
                    {catLabel(c)}
                  </Text>
                </Pressable>
              ))}
            </ScrollView>
      </Sheet>
    </Card>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  propRow: { flexDirection: "row", alignItems: "center", gap: 8,
             borderTopColor: C.border,
             borderTopWidth: StyleSheet.hairlineWidth,
             paddingVertical: 8 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 15,
           paddingHorizontal: 10, paddingVertical: 8 },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  catRow: { paddingVertical: 9, borderTopColor: C.border,
            borderTopWidth: StyleSheet.hairlineWidth },
});
