// One bill's configuration — rename, amount, cadence, next due,
// occurrence cap, envelope category, pin-to-Today, enable/disable,
// archive, delete forever. Same fields the web edit expander saves.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Switch, Text,
         TextInput, View } from "react-native";

import { Card, H } from "../components/ui";
import type { Bill, BillDetail, BillsData, BillsHistoryData } from "../lib/api";
import { patchList, patchQuery } from "../lib/cache";
import { useSession } from "../lib/session";
import { useViewer } from "../lib/viewer";
import { C } from "../lib/theme";
import { catLabel } from "../lib/pure";
import { errText } from "../lib/api";

export default function BillEdit() {
  // pamt/pcad/pdue prefill the form from a ledger fit ("edit with these
  // values" on the history screen) — the saved values, not the stored ones
  const { payee, pamt, pcad, pdue } = useLocalSearchParams<{
    payee: string; pamt?: string; pcad?: string; pdue?: string }>();
  const { client } = useSession();
  const viewer = useViewer();
  const qc = useQueryClient();
  const router = useRouter();
  const query = useQuery({
    queryKey: ["bill", payee],
    queryFn: () => client!.billsBill(String(payee)),
    enabled: !!client && !!payee,
  });
  const d = query.data;
  const [name, setName] = useState("");
  const [amount, setAmount] = useState("");
  const [cadence, setCadence] = useState("");
  const [nextDue, setNextDue] = useState("");
  const [cap, setCap] = useState("");
  const [category, setCategory] = useState("");
  const [matchCat, setMatchCat] = useState(false);
  const [showToday, setShowToday] = useState(false);
  // the fees that ride with this payment (the web's "Fees & add-ons")
  const [fees, setFees] = useState<{ tokens: string; amount: string;
                                      window_days: string }[]>([]);
  const [feesDirty, setFeesDirty] = useState(false);
  // the merchants this bill pays — its identity (the web's "Merchants"):
  // the ledger's rows under these names are the bill's charges
  const [merchants, setMerchants] = useState<string[]>([]);
  const [merchantsDirty, setMerchantsDirty] = useState(false);
  const [newMerchant, setNewMerchant] = useState("");
  const known = useQuery({ queryKey: ["merchants"],
                           queryFn: () => client!.merchants(),
                           enabled: !!client });
  const suggestions = useMemo(() => {
    const q = newMerchant.trim().toLowerCase();
    if (q.length < 2) return [] as string[];
    return (known.data?.merchants ?? [])
      .filter((m) => m.toLowerCase().includes(q) && !merchants.includes(m))
      .slice(0, 6);
  }, [newMerchant, known.data, merchants]);
  const addMerchant = (v: string) => {
    const t = v.trim();
    if (t && !merchants.includes(t) && merchants.length < 20) {
      setMerchants([...merchants, t]);
      setMerchantsDirty(true);
    }
    setNewMerchant("");
  };
  // the transaction category stamped on matched charges (web: "matched
  // charges are …") — a merchant that is several things
  const [txnCat, setTxnCat] = useState("");
  const isIncome = !!d?.income;
  // the picker's chips only render inside the non-income section below —
  // an income bill never shows them, so it never needs the fetch
  const cats = useQuery({ queryKey: ["categories"],
    queryFn: () => client!.categories(), enabled: !!client && !isIncome });
  // the "matched charges are categorized as" chip list — every category
  // minus the flow ones, deduped; recomputed only when the categories
  // query itself changes, not on every keystroke elsewhere on this form
  const txnCatOptions = useMemo(() => {
    const flow = new Set(cats.data?.plaid_flow ?? []);
    return [...new Set([...(cats.data?.categories ?? []),
                        ...(cats.data?.plaid_spend ?? [])])]
      .filter((c) => !flow.has(c));
  }, [cats.data]);
  const [err, setErr] = useState<string | null>(null);
  // seed once per payee, and never again — `d` re-renders on every
  // refetch (focusManager refetches on app-foreground), and reseeding
  // from it would silently revert whatever the user had typed since
  const seededPayee = useRef<string | null>(null);
  useEffect(() => {
    if (!d || seededPayee.current === payee) return;
    seededPayee.current = String(payee);
    setName(d.payee);
    setAmount(pamt ? String(pamt) : String(d.amount));
    setCadence(pcad ? String(pcad) : d.cadence);
    setNextDue(pdue ? String(pdue) : (d.next_due ?? ""));
    setCap(d.cap !== null ? String(d.cap) : "");
    setCategory(d.category ?? "");
    setMatchCat(d.match_category);
    setShowToday(d.show_today);
    setTxnCat(d.txn_category ?? "");
    setFees((d.companions ?? []).map((c) => ({ tokens: c.tokens,
      amount: String(c.amount), window_days: String(c.window_days) })));
    setFeesDirty(false);
    setMerchants(d.merchants ?? []);
    setMerchantsDirty(false);
  }, [d, payee, pamt, pcad, pdue]);
  const isEnv = cadence.startsWith("ENVELOPE");

  // every write here reaches the schedule, the calendar and Today — and
  // the two screens this one sits on: the payee's history underneath
  // (it shows the amount and health just changed) and this form's own
  // source, which a reopen within the stale window would re-seed from
  // the pre-save copy. A rename or a delete drops that copy outright.
  const done = (o: { gone?: boolean; renamedTo?: string } = {}) => {
    qc.invalidateQueries({ queryKey: ["bills"] });
    qc.invalidateQueries({ queryKey: ["calendar"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["payee-history", payee] });
    if (o.gone || (o.renamedTo && o.renamedTo !== payee))
      qc.removeQueries({ queryKey: ["bill", payee] });
    else qc.invalidateQueries({ queryKey: ["bill", payee] });
    if (o.renamedTo && o.renamedTo !== payee)
      qc.invalidateQueries({ queryKey: ["payee-history", o.renamedTo] });
    router.back();
  };
  const save = useMutation({
    // category + its pool flag only travel on envelope saves, so an
    // occurrence edit can never strip detection's category from the row
    mutationFn: () => client!.billsSave({
      payee: name.trim() || d!.payee, orig_payee: d!.payee,
      amount, cadence, next_due: nextDue || undefined,
      income: d!.income,
      ...(cadence !== "ENVELOPE:1"
        ? { cap: cap === "" ? null : cap } : {}),
      ...(isEnv ? { match_category: matchCat && !!category.trim(),
                    category: category.trim() || null }
                : { category: d!.category,
                    match_category: d!.match_category }),
      ...(d!.income ? {} : { show_today: showToday }),
      ...(txnCat !== (d!.txn_category ?? "") ? { txn_category: txnCat } : {}),
      // only sent when edited, so a save from elsewhere keeps the identity
      ...(merchantsDirty ? { merchants } : {}),
      ...(feesDirty ? { companions: fees
          .filter((f) => f.tokens.trim() && f.amount.trim())
          .map((f) => ({ tokens: f.tokens.trim(), amount: Number(f.amount),
                         window_days: Number(f.window_days || 3) })) } : {}) }),
    onSuccess: (r) => {
      // the screens behind this one show the saved values at once; the
      // refetches that follow only confirm them
      if (r.payee === d!.payee) {
        const amt = Number(amount);
        patchQuery<BillDetail>(qc, ["bill", payee], {
          amount: Number.isFinite(amt) ? amt : d!.amount, cadence,
          next_due: nextDue, show_today: showToday, txn_category: txnCat,
          ...(isEnv ? { category: category.trim() || null,
                        match_category: matchCat && !!category.trim() }
                    : { cap: cap === "" ? null : Number(cap) }) });
        // every window of the history page holds the same bill header
        qc.setQueriesData<BillsHistoryData>({ queryKey: ["payee-history", payee] },
          (old) => old && old.bill ? { ...old, bill: { ...old.bill,
            amount: Number.isFinite(amt) ? amt : old.bill.amount,
            cadence, due_on: nextDue || null } } : old);
      }
      done({ renamedTo: r.payee });
    },
    onError: (e) => setErr(errText(e)),
  });
  const toggle = useMutation({
    mutationFn: () => client!.billsToggle(d!.payee),
    onSuccess: (r) => {
      patchQuery<BillDetail>(qc, ["bill", payee], { disabled: r.disabled });
      patchList<BillsData, Bill>(qc, ["bills"], "bills",
        (b) => b.payee === d!.payee, { disabled: r.disabled });
      done();
    },
    onError: (e) => setErr(errText(e)),
  });
  const del = useMutation({
    mutationFn: (mode: "archive" | "purge") =>
      client!.billsDelete(d!.payee, mode),
    onSuccess: () => done({ gone: true }),
    onError: (e) => setErr(errText(e)),
  });
  const askPurge = () => Alert.alert("Delete forever",
    "Permanently delete this bill? No archive copy is kept (ledger "
    + "transactions are untouched).",
    [{ text: "Delete forever", style: "destructive",
       onPress: () => del.mutate("purge") },
     { text: "Cancel", style: "cancel" }]);
  // one destructive write at a time — a slow request invites a second
  // click (double purge; double toggle silently nets to no change)
  const busy = toggle.isPending || del.isPending;

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}>
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {query.isError && !d && (
        <Text style={[s.center, { color: C.bad }]}
              onPress={() => query.refetch()}>
          Couldn't load this bill — tap to retry.
        </Text>
      )}
      {d && (
        <>
          <Card>
            <H>{d.payee}</H>
            <Text style={s.lbl}>
              Payee (renaming changes ledger matching)
            </Text>
            <TextInput style={s.input} value={name} onChangeText={setName} />
            <Text style={s.lbl}>Amount</Text>
            <TextInput style={s.input} value={amount} onChangeText={setAmount}
                       keyboardType="decimal-pad" />
            <Text style={s.lbl}>Cadence</Text>
            <View style={s.cadRow}>
              {/* a ledger-fit prefill may carry a cadence outside the
                  standard list — keep it visible and selected */}
              {[...d.cadences,
                ...(cadence && !d.cadences.some(([k]) => k === cadence)
                  ? [[cadence, cadence.toLowerCase().replace(":", " × ")] as
                      [string, string]]
                  : [])].map(([key, label]) => (
                <Pressable key={key}
                           style={[s.cad, cadence === key && s.cadOn]}
                           onPress={() => setCadence(key)}>
                  <Text style={{ color: cadence === key ? C.text : C.mut,
                                 fontSize: 12 }}>{label}</Text>
                </Pressable>
              ))}
            </View>
            <Text style={s.lbl}>Next due (YYYY-MM-DD)</Text>
            <TextInput style={s.input} value={nextDue}
                       onChangeText={setNextDue} autoCapitalize="none" />
            {/* an occurrence count, not a dollar pool — envelopes have
                no occurrence cap (the amount IS their pool) */}
            {!isEnv && (<>
              <Text style={s.lbl}>
                Occurrence cap (max per month, blank = none)
              </Text>
              <TextInput style={s.input} value={cap} onChangeText={setCap}
                         keyboardType="number-pad" />
            </>)}
            {isEnv && (<>
              <Text style={s.lbl}>Envelope category (blank = none)</Text>
              <TextInput style={s.input} value={category}
                         onChangeText={setCategory} autoCapitalize="none" />
              <View style={s.switchRow}>
                <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
                  Count all {category.trim() || "category"} spending
                  toward the pool, even when the merchant doesn&apos;t
                  match
                </Text>
                <Switch value={matchCat && !!category.trim()}
                        disabled={!category.trim()}
                        onValueChange={setMatchCat}
                        trackColor={{ true: C.accent, false: C.hover }}
                        thumbColor={C.text} />
              </View>
            </>)}
            {!d.income && (<>
              <Text style={s.lbl}>Matched charges are categorized as</Text>
              <Text style={{ color: C.mut, fontSize: 12, marginBottom: 6 }}>
                For a merchant that is several things — other charges there
                keep their own category; a category you set on a row yourself
                always wins.
              </Text>
              <View style={s.cadRow}>
                <Pressable style={[s.cad, !txnCat && s.cadOn]}
                           onPress={() => setTxnCat("")}>
                  <Text style={{ color: C.text, fontSize: 13 }}>
                    merchant&apos;s category</Text>
                </Pressable>
                {txnCatOptions.map((c) => (
                  <Pressable key={c} style={[s.cad, txnCat === c && s.cadOn]}
                             onPress={() => setTxnCat(c)}>
                    <Text style={{ color: C.text, fontSize: 13 }}>
                      {catLabel(c)}</Text>
                  </Pressable>
                ))}
              </View>
            </>)}
            {!d.income && (
            <View style={s.switchRow}>
              <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
                Pin to Today (its own card + daily email)
              </Text>
              <Switch value={showToday} onValueChange={setShowToday}
                      trackColor={{ true: C.accent, false: C.hover }}
                      thumbColor={C.text} />
            </View>
            )}
            <View style={{ marginTop: 8 }}>
              <Text style={{ color: C.mut, fontSize: 12 }}>
                Merchants — the merchants this bill pays. Its charges are
                the ledger&apos;s rows under these names, whatever the
                bank&apos;s wording; empty = match by the words of the
                bill&apos;s name.
              </Text>
              <View style={[s.cadRow, { marginTop: 6 }]}>
                {merchants.map((m) => (
                  <Pressable key={m} style={[s.cad, s.cadOn]}
                             onPress={() => { setMerchantsDirty(true);
                               setMerchants(merchants.filter((x) => x !== m)); }}>
                    <Text style={{ color: C.text, fontSize: 13 }}>{m}  ×</Text>
                  </Pressable>
                ))}
              </View>
              {merchants.length < 20 && (
                <View style={{ flexDirection: "row", gap: 6,
                               alignItems: "center", marginTop: 6 }}>
                  <TextInput style={[s.input, { flex: 1 }]} value={newMerchant}
                             placeholder="add a merchant"
                             placeholderTextColor={C.mut}
                             onChangeText={setNewMerchant}
                             onSubmitEditing={() => addMerchant(newMerchant)} />
                  <Pressable onPress={() => addMerchant(newMerchant)}>
                    <Text style={{ color: C.accent, fontSize: 13 }}>add</Text>
                  </Pressable>
                </View>
              )}
              {suggestions.length > 0 && (
                <View style={s.cadRow}>
                  {suggestions.map((m) => (
                    <Pressable key={m} style={s.cad} onPress={() => addMerchant(m)}>
                      <Text style={{ color: C.text, fontSize: 13 }}>{m}</Text>
                    </Pressable>
                  ))}
                </View>
              )}
            </View>
            {!d.income && !isEnv && (
            <View style={{ marginTop: 8 }}>
              <Text style={{ color: C.mut, fontSize: 12 }}>
                Fees &amp; add-ons — a fee that rides with this payment
                (a processor&apos;s convenience fee) counts with the bill
                when it lands within the window of the main charge.
              </Text>
              {fees.map((f, i) => (
                <View key={i} style={{ flexDirection: "row", gap: 6,
                                       alignItems: "center", marginTop: 6 }}>
                  <TextInput style={[s.input, { flex: 2 }]} value={f.tokens}
                             placeholder="words on the charge"
                             placeholderTextColor={C.mut}
                             onChangeText={(v) => { setFeesDirty(true);
                               setFees(fees.map((x, j) =>
                                 j === i ? { ...x, tokens: v } : x)); }} />
                  <TextInput style={[s.input, { width: 64 }]} value={f.amount}
                             placeholder="$" keyboardType="decimal-pad"
                             placeholderTextColor={C.mut}
                             onChangeText={(v) => { setFeesDirty(true);
                               setFees(fees.map((x, j) =>
                                 j === i ? { ...x, amount: v } : x)); }} />
                  <TextInput style={[s.input, { width: 44 }]}
                             value={f.window_days} keyboardType="number-pad"
                             placeholderTextColor={C.mut}
                             onChangeText={(v) => { setFeesDirty(true);
                               setFees(fees.map((x, j) =>
                                 j === i ? { ...x, window_days: v } : x)); }} />
                  <Text style={{ color: C.mut, fontSize: 12 }}>d</Text>
                  <Pressable onPress={() => { setFeesDirty(true);
                               setFees(fees.filter((_, j) => j !== i)); }}>
                    <Text style={{ color: C.bad, fontSize: 12 }}>remove</Text>
                  </Pressable>
                </View>
              ))}
              {fees.length < 8 && (
                <Pressable onPress={() => { setFeesDirty(true);
                             setFees([...fees, { tokens: "", amount: "",
                                                 window_days: "3" }]); }}
                           style={{ marginTop: 6 }}>
                  <Text style={{ color: C.accent, fontSize: 13 }}>
                    + add a fee</Text>
                </Pressable>
              )}
            </View>
            )}
            {err ? (
              <Text style={{ color: C.bad, fontSize: 12 }}
                    onPress={() => setErr(null)}>{err}</Text>
            ) : null}
            {!viewer && (<>
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 8,
                           marginTop: 12 }}>
              <Pressable style={[s.btn, save.isPending && { opacity: 0.5 }]}
                         disabled={save.isPending}
                         onPress={() => save.mutate()}>
                <Text style={s.btnText}>Save</Text>
              </Pressable>
              <Pressable style={[s.btn, s.btnQuiet,
                           busy && { opacity: 0.5 }]}
                         disabled={busy}
                         onPress={() => toggle.mutate()}>
                <Text style={[s.btnText, { color: d.disabled
                                ? C.good : C.warn }]}>
                  {d.disabled ? "Enable" : "Disable"}
                </Text>
              </Pressable>
              <Pressable style={[s.btn, s.btnQuiet,
                           busy && { opacity: 0.5 }]}
                         disabled={busy}
                         onPress={() => del.mutate("archive")}>
                <Text style={[s.btnText, { color: C.warn }]}>Archive</Text>
              </Pressable>
              <Pressable style={[s.btn, s.btnQuiet,
                           busy && { opacity: 0.5 }]}
                         disabled={busy}
                         onPress={askPurge}>
                <Text style={[s.btnText, { color: C.bad }]}>
                  Delete forever
                </Text>
              </Pressable>
            </View>
            <Text style={[s.footer, { textAlign: "left", marginTop: 8 }]}>
              Disable excludes the bill from the budget while keeping it
              here. Archive keeps a restorable copy under Bills.
            </Text>
            </>)}
          </Card>
        </>
      )}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  lbl: { color: C.mut, fontSize: 12, marginTop: 10, marginBottom: 4 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 15,
           paddingHorizontal: 10, paddingVertical: 8 },
  cadRow: { flexDirection: "row", flexWrap: "wrap", gap: 6 },
  cad: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
         borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  cadOn: { borderColor: C.accent, backgroundColor: C.hover },
  switchRow: { flexDirection: "row", alignItems: "center", gap: 8,
               marginTop: 12 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 18, paddingVertical: 9 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 14, fontWeight: "600" },
  footer: { color: C.mut, fontSize: 12, textAlign: "center", marginTop: 12 },
});
