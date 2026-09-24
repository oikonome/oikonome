// One transaction, full detail — pushed from any ledger row, and the
// home of the first mobile WRITES: category override (with the same
// one/this-merchant scope question the web asks) and the
// reimbursement flag. The route carries only the row's id; the row
// itself arrives through the in-memory hand-off in api.ts (see
// setHandedOffTxn — a deep link cannot forge it). Edits update local
// state, patch the same row in every cached ledger page, and invalidate
// only the queries the write could really have moved.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocalSearchParams, useRouter } from "expo-router";
import { useMemo, useState } from "react";
import { Alert, Modal, Pressable, ScrollView, StyleSheet, Text, TextInput,
         View } from "react-native";

import * as DocumentPicker from "expo-document-picker";
import * as ImagePicker from "expo-image-picker";

import { Card, H, KV } from "../components/ui";
import { errText, getHandedOffTxn, SplitPart, TodayFull, Txn,
         TxnPage } from "../lib/api";
import { patchQueries, removeFromList } from "../lib/cache";
import { categoryForServer, CLEAR_CATEGORY, catLabel, filterCategories,
         isCategoryReset } from "../lib/pure";
import { useSession } from "../lib/session";
import { useDemo, useViewer } from "../lib/viewer";
import { C, mmddyy, money } from "../lib/theme";
import MerchantAvatar from "../components/merchant-avatar";

// A pending authorization the bank never posted and never released. Card
// holds clear within days, so a row still pending after two weeks is
// stuck, not in flight — the same line the web draws and the same one
// the server enforces; this only decides whether to OFFER the control.
// Parsed field by field because new Date("2026-08-13") is UTC and reads
// a day early west of Greenwich, which would offer it a day too soon.
const STUCK_PENDING_DAYS = 14;
const stuckPending = (t: Txn) => {
  if (!t.pending) return false;
  const [y, m, d] = t.date.slice(0, 10).split("-").map(Number);
  if (!y || !m || !d) return false;
  return Date.now() - new Date(y, m - 1, d).getTime()
    > STUCK_PENDING_DAYS * 86400000;
};

export default function TxnDetail() {
  const { id } = useLocalSearchParams<{ id?: string }>();
  const { client } = useSession();
  const router = useRouter();
  const qc = useQueryClient();
  const initial = getHandedOffTxn(id ? String(id) : undefined);
  const [txn, setTxn] = useState<Txn | null>(initial);
  const [picking, setPicking] = useState(false);
  const [filter, setFilter] = useState("");
  const [err, setErr] = useState<string | null>(null);
  // one charge, several categories — the split editor's draft (null =
  // closed) and, while its category sheet is open, which part it serves
  const [splitDraft, setSplitDraft] =
    useState<{ category: string; amount: string }[] | null>(null);
  const [pickFor, setPickFor] = useState<number | null>(null);

  const cats = useQuery({
    queryKey: ["categories"],
    queryFn: () => client!.categories(),
    enabled: !!client && (picking || pickFor !== null),
  });
  // the payee's whole story — same payload the web history page renders
  const hist = useQuery({
    // the history page's default window, so the two share one cache entry
    queryKey: ["payee-history", initial?.payee, "all"],
    queryFn: () => client!.billsHistory(initial!.payee, "all"),
    enabled: !!client && !!initial?.payee,
  });
  const viewer = useViewer();
  // the demo instance refuses merchant-wide category writes (its login is
  // shared) — the app hides the "All" choice there rather than offering a
  // button that 403s after the scope question. Single-row writes still work.
  const demo = useDemo();
  const [noteDraft, setNoteDraft] = useState<string | null>(null);
  const note = useMutation({
    mutationFn: (n: string) => client!.setTxnNote(txn!.id, n),
    onSuccess: (_d, n) => {
      setTxn((x) => (x ? { ...x, note: n || null } : x));
      setNoteDraft(null);
      // a note moves no money: the row is patched, nothing is refetched
      patchRow({ note: n || null });
    },
    onError: (e) => setErr(errText(e)),
  });
  const biz = useMutation({
    mutationFn: (on: boolean) => client!.bizFlag(txn!.id, on),
    onSuccess: (_d, on) => {
      setTxn((x) => (x ? { ...x, biz_flag: on } : x));
      patchRow({ biz_flag: on });
      // business rows leave the household totals and join the flagged
      // ledger, so both lists' figures really change
      invalidateLedgers();
      qc.invalidateQueries({ queryKey: ["business"] });
    },
    onError: (e) => setErr(errText(e)),
  });
  const receipts = useQuery({
    queryKey: ["receipts", initial?.id],
    queryFn: () => client!.receipts(initial!.id),
    enabled: !!client && !!initial?.id,
    refetchInterval: (query) =>
      query.state.data?.receipts.some(
        (r) => r.status === "parsing" || r.status === "uploaded")
        ? 3000 : false,
  });
  const upload = useMutation({
    mutationFn: ({ uri, kind, name, type }: {
      uri: string; kind: "receipt" | "check";
      name?: string; type?: string;
    }) => client!.receiptUpload(txn!.id, uri, kind, name, type),
    onSuccess: () => { receipts.refetch(); invalidateLedgers(); },
    onError: (e) => setErr(errText(e)),
  });
  const delReceipt = useMutation({
    mutationFn: (id: string) => client!.receiptDelete(id),
    // the server has already dropped it; the row leaves the list now
    // instead of after the refetch
    onSuccess: (_d, rid) => removeFromList<{ receipts: { id: string }[] },
                                           { id: string }>(
      qc, ["receipts", initial?.id], "receipts", (r) => r.id === rid),
    onError: (e) => setErr(errText(e)),
  });
  const snap = async (fromCamera: boolean,
                      kind: "receipt" | "check" = "receipt") => {
    const fn = fromCamera
      ? ImagePicker.launchCameraAsync
      : ImagePicker.launchImageLibraryAsync;
    if (fromCamera) {
      const perm = await ImagePicker.requestCameraPermissionsAsync();
      if (!perm.granted) return;
    }
    const res = await fn({ mediaTypes: ["images"], quality: 0.8 });
    if (!res.canceled && res.assets[0])
      upload.mutate({ uri: res.assets[0].uri, kind });
  };
  // emailed receipts are PDFs — the server and the web client take them
  // (they rasterize for parsing), so the phone needs a door for one too:
  // neither the camera nor the photo library can supply a PDF. Checks
  // arrive the same way — a bank's image download or a scanner's PDF —
  // so the kind travels with the pick, as it does on the web, where one
  // file input accepts image/* and .pdf for each kind.
  const pickPdf = async (kind: "receipt" | "check" = "receipt") => {
    const res = await DocumentPicker.getDocumentAsync({
      type: "application/pdf", copyToCacheDirectory: true });
    if (!res.canceled && res.assets[0])
      upload.mutate({ uri: res.assets[0].uri, kind,
                      name: res.assets[0].name || `${kind}.pdf`,
                      type: "application/pdf" });
  };
  const parse = useMutation({
    mutationFn: (id: string) => client!.receiptParse(id),
    onSuccess: () => receipts.refetch(),
    onError: (e) => setErr(errText(e)),
  });
  const viewImage = async (id: string, mime: string, optimized: boolean) => {
    const ext = mime.includes("pdf") ? "pdf"
      : mime.includes("png") ? "png" : "jpg";
    try {
      await client!.download(
        `/api/receipts/${encodeURIComponent(id)}/image`
        + (optimized ? "?variant=optimized" : ""),
        `receipt-${id.slice(0, 8)}.${ext}`);
    } catch (e) {
      setErr(errText(e));
    }
  };
  const entities = useQuery({
    queryKey: ["business-summary"],
    queryFn: () => client!.businessSummary(),
    enabled: !!client && !!txn?.biz_flag,
  });
  const [assigned, setAssigned] = useState<string | null | undefined>();
  const assign = useMutation({
    mutationFn: (entityId: string | null) =>
      client!.txnAssignEntity(txn!.id, entityId),
    onSuccess: (_d, entityId) => {
      setAssigned(entityId);
      qc.invalidateQueries({ queryKey: ["business"] });
    },
    onError: (e) => setErr(errText(e)),
  });
  // the same row sits in every cached ledger page (each filter set is
  // its own ["transactions", params] entry) and in Today's recent pane;
  // flip it everywhere at once so the list behind this screen already
  // shows the change when the user goes back
  const patchRow = (partial: Partial<Txn>) => {
    const id = txn!.id;
    const flip = (rows: Txn[]) =>
      rows.map((r) => r.id === id ? { ...r, ...partial } : r);
    patchQueries<TxnPage>(qc, ["transactions"],
      (o) => ({ ...o, rows: flip(o.rows) }));
    patchQueries<TodayFull>(qc, ["today"],
      (o) => o.recent ? { ...o, recent: flip(o.recent) } : o);
  };
  // writes that move spend: the list totals and the verdict are
  // recomputed server-side, so the rows are patched above and the
  // figures refetch behind them
  const invalidateLedgers = () => {
    qc.invalidateQueries({ queryKey: ["transactions"] });
    qc.invalidateQueries({ queryKey: ["today"] });
  };
  // the row is gone, not changed — take it out of the same caches
  // patchRow flips, so the list behind this screen has already dropped
  // it when the user lands back on it
  const dropRow = () => {
    const id = txn!.id;
    const drop = (rows: Txn[]) => rows.filter((r) => r.id !== id);
    patchQueries<TxnPage>(qc, ["transactions"],
      (o) => ({ ...o, rows: drop(o.rows) }));
    patchQueries<TodayFull>(qc, ["today"],
      (o) => o.recent ? { ...o, recent: drop(o.recent) } : o);
  };
  // Retire a pending row the bank never resolved — the web's "Remove
  // stuck pending". Sync cannot do it on its own (a row missing from one
  // feed response is not proof the charge was cancelled), so it is a
  // hand action; the server refuses anything not pending or younger than
  // two weeks and its refusal text is what the alert says.
  const retire = useMutation({
    mutationFn: () => client!.retirePending(txn!.id),
    onSuccess: () => {
      dropRow();
      invalidateLedgers();
      // this screen is now showing a transaction that no longer exists
      router.back();
    },
    onError: (e) => Alert.alert("Couldn't remove it", errText(e)),
  });
  // A reset ("Use automatic") is the one category write whose RESULT the
  // client cannot compute: dropping the override hands the row back to the
  // automatic layers, and what they say — the bank's own label, a bill's
  // stamp, a merchant rule — is known only to the server. The web table
  // skips its optimistic patch there and lets the ledger refetch it is
  // bound to bring the answer back. This screen has no query behind it (its
  // row is handed over in memory), so nothing would ever correct a guess:
  // it reads the row back itself and adopts the server's whole answer —
  // the category, the provenance line under it, and the override flag the
  // "Use automatic" button is gated on.
  const adoptServerRow = async () => {
    const fresh = await client!.txnById(txn!.id, txn!.date);
    // not in its own day's listing: leave what is on screen rather than
    // replace it with a guess — the write itself landed either way
    if (!fresh) return;
    setTxn(fresh);
    patchRow(fresh);
  };
  const setCat = useMutation({
    // the sentinel→empty-string mapping lives in pure.ts so the wire
    // call and the local echo can never disagree
    mutationFn: ({ category, scope }:
        { category: string; scope: "one" | "all" }) =>
      client!.setCategory(txn!.id, categoryForServer(category), scope),
    // the picker closes and the row flips on the tap, not on the round
    // trip; a refused write puts the old category back. A reset has no
    // echo to make (see adoptServerRow) — the row keeps the category it
    // is showing until the server's own answer arrives, exactly as the
    // web row does while its refetch is in flight.
    onMutate: (v) => {
      const before = txn;
      setPicking(false);
      if (!isCategoryReset(categoryForServer(v.category)))
        setTxn((x) => (x ? { ...x,
          category: categoryForServer(v.category) } : x));
      return { before };
    },
    onSuccess: (_d, v) => {
      if (isCategoryReset(categoryForServer(v.category))) {
        invalidateLedgers();
        // the write landed — say so, since a failed read-back leaves the
        // old category on screen and would otherwise read as a dead tap
        void adoptServerRow().catch((e) =>
          setErr(`Reset saved — couldn't refresh this screen: ${errText(e)}`));
        return;
      }
      patchRow({ category: categoryForServer(v.category) });
      // the category decides whether the row can be split (and so does
      // the account type and the bank's detail, which only the server
      // weighs), so read the row back rather than keep the `splittable`
      // this screen opened with. If the read-back fails, the category on
      // screen is already right and the split door still refuses a
      // non-spending row itself.
      void adoptServerRow().catch(() => {});
      if (v.scope === "all") {
        // a merchant rule rewrites rows in every month, and the Merchants
        // detail sheet shows the merchant's category and rule (the web
        // invalidates it too)
        invalidateLedgers();
        qc.invalidateQueries({ queryKey: ["merchant-detail"] });
      } else {
        // one row's category still moves the verdict's trailing-month
        // tolerance and typical spend, and Today may be viewing the
        // month the row is in — so it refetches either way (cheap)
        invalidateLedgers();
      }
    },
    onError: (e, _v, ctx) => {
      if (ctx?.before) setTxn(ctx.before);
      setErr(errText(e));
    },
  });

  // the split's parts count in every per-category figure — the verdict,
  // the Spending categories, both lenses — so those refetch behind the
  // patched row (the web SplitPanel invalidates the same)
  const splitLanded = (split: SplitPart[] | null) => {
    setTxn((x) => (x ? { ...x, split } : x));
    patchRow({ split });
    setSplitDraft(null);
    invalidateLedgers();
    qc.invalidateQueries({ queryKey: ["report"] });
    qc.invalidateQueries({ queryKey: ["lens-month"] });
    qc.invalidateQueries({ queryKey: ["lens-year"] });
  };
  const splitSave = useMutation({
    mutationFn: (parts: SplitPart[]) => client!.txnSplitSet(txn!.id, parts),
    onSuccess: (r) => splitLanded(r.split),
    onError: (e) => Alert.alert("Couldn't save the split", errText(e)),
  });
  const splitClear = useMutation({
    mutationFn: () => client!.txnSplitClear(txn!.id),
    onSuccess: () => splitLanded(null),
    onError: (e) => Alert.alert("Couldn't remove the split", errText(e)),
  });

  const reimb = useMutation({
    mutationFn: (arg: { flag: boolean; partial?: boolean }) =>
      arg.flag ? client!.reimbFlag(txn!.id, { partial: arg.partial })
               : client!.reimbUnflag(txn!.id),
    onSuccess: (_d, arg) => {
      setTxn((x) => (x ? { ...x, reimb_flag: arg.flag } : x));
      patchRow({ reimb_flag: arg.flag });
      invalidateLedgers();
      // the Reimburse screen's pending list gained or lost this charge
      qc.invalidateQueries({ queryKey: ["reimburse"] });
    },
    onError: (e) => setErr(errText(e)),
  });
  // capitalize / reimburse a personally-paid business expense —
  // assigns AND records the equity movement (web's __bizc__/__bizr__)
  const equity = useMutation({
    mutationFn: ({ entityId, mode }:
        { entityId: string; mode: "contribute" | "reimburse" }) =>
      client!.equityReimburse(entityId, txn!.id, mode),
    onSuccess: (_d, v) => {
      setAssigned(v.entityId);
      qc.invalidateQueries({ queryKey: ["business"] });
      Alert.alert("Recorded", v.mode === "contribute"
        ? "Assigned to the business as an owner contribution."
        : "Assigned to the business, reimbursement recorded.");
    },
    onError: (e) => setErr(errText(e)),
  });
  const askFlag = () => {
    Alert.alert("Reimbursement", "Expect the whole amount back, or part?",
      [{ text: "Whole amount",
         onPress: () => reimb.mutate({ flag: true }) },
       { text: "Partial",
         onPress: () => reimb.mutate({ flag: true, partial: true }) },
       { text: "Cancel", style: "cancel" }]);
  };

  // spending + the flow set — TRANSFER_*/INCOME_* decide what counts
  // as spend, so the row picker must reach them (web's second optgroup)
  const catList = useMemo(() => filterCategories([...new Set([
    ...(cats.data?.categories ?? []),
    ...(cats.data?.plaid_spend ?? []),
    ...(cats.data?.plaid_flow ?? []),
  ])], filter), [cats.data, filter]);

  if (!txn) {
    return <Text style={[s.big, { padding: 20 }]}>Transaction not found.</Text>;
  }
  const inflow = txn.amount < 0;
  const flagged = !!(txn.reimb || txn.reimb_flag);
  // the server says whether the split door takes this row (the spend
  // test the rollups use); the web gates its ✂ action on the same field
  const canSplit = !viewer && !!txn.splittable;

  // "All" is the same merchant-wide write as Bills → "Apply to whole
  // merchant": a user rule outranks the bank's transfer label, so
  // bank-labelled transfers/income move into spend. The web asks the
  // preview first and names that consequence before writing; so does
  // this. The preview is advisory — owner-only, so a member's write
  // still goes through when it cannot be read.
  const applyAll = async (category: string) => {
    let flow_rows = 0, skipped_override = 0;
    try {
      // family: false — this row's "All" writes scope "all" on ONE
      // canonical merchant, so the preview counts the same single-
      // canonical scope (bill-history keeps family scope)
      const p = await client!.merchantCategoryPreview(
        txn.payee, categoryForServer(category), { family: false });
      flow_rows = p.flow_rows ?? 0;
      skipped_override = p.skipped_override ?? 0;
    } catch {
      // advisory off the demo — but on the demo instance a refusal here
      // IS the write guard, and proceeding would only 403 after it
      if (demo) {
        setErr("Not available on the demo instance — synthetic data only.");
        return;
      }
    }
    if (flow_rows === 0) { setCat.mutate({ category, scope: "all" }); return; }
    Alert.alert(`All ${txn.payee}`,
      `${flow_rows} of these row${flow_rows === 1 ? " is" : "s are"} `
      + "labelled by your bank as a transfer, income or loan payment and "
      + "WILL move too — your merchant rule outranks that label. They "
      + "start counting as spending."
      + (skipped_override
        ? ` ${skipped_override} row${skipped_override === 1 ? " has"
            : "s have"} a category you set by hand and will be left alone.`
        : ""), [
      { text: "Yes, recategorize",
        onPress: () => setCat.mutate({ category, scope: "all" }) },
      { text: "Cancel", style: "cancel" },
    ]);
  };
  const chooseCategory = (category: string) => {
    // the split editor asked: set that part and close, no scope question
    if (pickFor !== null) {
      const i = pickFor;
      setSplitDraft((d) => d ? d.map((p, j) => j === i ? { ...p, category } : p) : d);
      setPickFor(null);
      setFilter("");
      return;
    }
    // the demo refuses the merchant-wide write, so the scope question
    // has one honest answer there — offering "All" would only 403
    if (demo) {
      Alert.alert(catLabel(category),
        "“Just this one” writes no rule. Merchant-wide recategorize is "
        + "not available on the demo instance — synthetic data only.", [
        { text: "Just this one",
          onPress: () => setCat.mutate({ category, scope: "one" }) },
        { text: "Cancel", style: "cancel" },
      ]);
      return;
    }
    // "all" teaches the merchant — never applied silently (a generic
    // payee claiming dozens of rows is exactly how that goes wrong)
    Alert.alert(catLabel(category),
      "“Just this one” writes no rule — it never appears on the "
      + "Rules page. “All” also teaches this merchant.", [
      { text: "Just this one",
        onPress: () => setCat.mutate({ category, scope: "one" }) },
      { text: `All ${txn.payee}`,
        onPress: () => { void applyAll(category); } },
      { text: "Cancel", style: "cancel" },
    ]);
  };

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}>
      <Card>
        <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
          <MerchantAvatar name={txn.payee} logo={txn.merchant_logo} size={40} />
          <Text style={[s.payee, { flex: 1 }]}>{txn.payee}</Text>
        </View>
        <Text style={[s.big, inflow && { color: C.good }]}>
          {inflow ? `+${money(-txn.amount)}` : money(txn.amount)}
        </Text>
        <KV k="Date" v={mmddyy(txn.date)} />
        <KV k="Account" v={txn.account || "—"} />
        {/* what the bank knows (the web's row detail): only facts a source
            supplied; nothing for a row that has none */}
        {(txn.location_city || txn.location_region) ? (
          <KV k="Location" v={[txn.location_city, txn.location_region]
            .filter(Boolean).join(", ")
            + (txn.location_store ? ` · store ${txn.location_store}` : "")} />
        ) : null}
        {(txn.payment_channel || txn.payment_processor) ? (
          <KV k="Paid" v={[txn.payment_channel,
            txn.payment_processor ? `via ${txn.payment_processor}` : ""]
            .filter(Boolean).join(" · ")} />
        ) : null}
        {txn.check_number ? <KV k="Check" v={`#${txn.check_number}`} /> : null}
        {txn.authorized_date && txn.authorized_date !== txn.date ? (
          <KV k="Authorized" v={mmddyy(txn.authorized_date)} />
        ) : null}
        {txn.mcc ? <KV k="Merchant code" v={`MCC ${txn.mcc}`} /> : null}
        {txn.category_plaid ? (
          <KV k="Plaid says" v={catLabel(txn.category_plaid).toLowerCase()
            + (txn.category_plaid_confidence
               ? ` · ${catLabel(txn.category_plaid_confidence).toLowerCase()}`
               : "")} tone="mut" />
        ) : null}
        {txn.bank_text ? <KV k="Bank text" v={txn.bank_text} tone="mut" /> : null}
        {txn.entity ? (
          <View style={{ flexDirection: "row", justifyContent: "space-between",
                         alignItems: "center", paddingVertical: 4 }}>
            <Text style={s.mut}>Business</Text>
            <View style={{ backgroundColor: "rgba(160,120,220,.18)",
                           borderRadius: 999, paddingHorizontal: 8,
                           paddingVertical: 2 }}>
              <Text style={{ color: C.entity, fontSize: 12, fontWeight: "600" }}>
                {txn.entity}</Text>
            </View>
          </View>
        ) : null}
        {txn.pending ? <KV k="Status" v="pending" tone="warn" /> : null}
        {txn.recurring_bill ? (
          <View style={{ flexDirection: "row",
                         justifyContent: "space-between" }}>
            <Text style={s.mut}>Recurring bill</Text>
            <Text style={{ color: C.accent, fontSize: 13 }}
                  onPress={() => router.push({ pathname: "/bill-history",
                    params: { payee: txn.recurring_bill! } } as never)}>
              {txn.recurring_bill} ›
            </Text>
          </View>
        ) : null}
        {txn.item_summary ? <KV k="Items" v={txn.item_summary} /> : null}
        {txn.note ? <KV k="Note" v={txn.note} /> : null}
      </Card>

      <Card>
        <View style={s.actRow}>
          <View style={{ flex: 1 }}>
            <Text style={s.mut}>Category</Text>
            <Text style={s.actVal}>{catLabel(txn.category) || "—"}</Text>
            {txn.category_why ? (
              <Text style={{ color: C.mut, fontSize: 12, marginTop: 2 }}>
                {txn.category_why}
              </Text>
            ) : null}
          </View>
          {!viewer && (
            <View style={{ gap: 6, alignItems: "flex-end" }}>
              <Pressable style={s.btn} onPress={() => setPicking(true)}>
                <Text style={s.btnText}>Change</Text>
              </Pressable>
              {/* a category you set yourself goes back to the automatic
                  layer in one tap, without opening the picker for its
                  reset entry (web: the ↺ beside the category) */}
              {txn.override_manual ? (
                <Pressable style={s.btn}
                           onPress={() => setCat.mutate({
                             category: CLEAR_CATEGORY, scope: "one" })}
                           accessibilityLabel="use the automatic category">
                  <Text style={s.btnText}>Use automatic</Text>
                </Pressable>
              ) : null}
            </View>
          )}
        </View>
        <View style={[s.actRow, { marginTop: 10 }]}>
          <View style={{ flex: 1 }}>
            <Text style={s.mut}>Reimbursement</Text>
            <Text style={s.actVal}>
              {txn.reimb ? "paired" : flagged ? "flagged — awaiting payback"
                                             : "not flagged"}
            </Text>
            {flagged && !txn.reimb && (
              <Text style={{ color: C.accent, fontSize: 12 }}
                    onPress={() => router.push("/reimburse" as never)}>
                mark reimbursed — pick the deposit ›
              </Text>
            )}
          </View>
          {!txn.reimb && !viewer && (
            <Pressable style={[s.btn, flagged && s.btnQuiet]}
                       disabled={reimb.isPending}
                       onPress={() => (flagged
                         ? reimb.mutate({ flag: false }) : askFlag())}>
              <Text style={[s.btnText, flagged && { color: C.mut }]}>
                {flagged ? "Unflag" : "Flag"}
              </Text>
            </Pressable>
          )}
        </View>
        {err ? <Text style={{ color: C.bad, fontSize: 12, marginTop: 8 }}
                     onPress={() => setErr(null)}>{err}</Text> : null}
      </Card>

      {/* one charge, several categories — the parts count where the
          category above would (the web's ✂ split panel). Offered on a
          row the server marks splittable — spending the rollups count, never
          a refund, transfer or card payment; viewers read the parts, plain. */}
      {(txn.split?.length || canSplit) ? (
      <Card>
        <View style={s.actRow}>
          <View style={{ flex: 1 }}>
            <Text style={s.mut}>Split across categories</Text>
            {txn.split?.length ? txn.split.map((p, i) => (
              <Text key={i} style={s.actVal}>
                <Text style={s.mut}>{money(Math.abs(p.amount))}</Text>
                {"  "}{catLabel(p.category)}
              </Text>
            )) : (
              <Text style={s.mut}>
                not split — the whole charge counts as {catLabel(txn.category)}
              </Text>
            )}
          </View>
          {!viewer && splitDraft === null && (
            <View style={{ gap: 6, alignItems: "flex-end" }}>
              {canSplit && (
              <Pressable style={s.btn} onPress={() => setSplitDraft(
                txn.split?.length
                  ? txn.split.map((p) => ({ category: p.category,
                                            amount: Math.abs(p.amount).toFixed(2) }))
                  // start from the row's own category and one empty line
                  : [{ category: txn.category_override || txn.category_key || "",
                       amount: Math.abs(txn.amount).toFixed(2) },
                     { category: "", amount: "" }])}>
                <Text style={s.btnText}>{txn.split?.length ? "Edit" : "Split…"}</Text>
              </Pressable>
              )}
              {txn.split?.length ? (
                <Pressable style={[s.btn, s.btnQuiet]} disabled={splitClear.isPending}
                           onPress={() => splitClear.mutate()}>
                  <Text style={[s.btnText, { color: C.mut }]}>Remove</Text>
                </Pressable>
              ) : null}
            </View>
          )}
        </View>
        {splitDraft !== null && (() => {
          const total = Math.round(Math.abs(txn.amount) * 100);
          const sum = splitDraft.reduce((a, p) => {
            const v = Number(p.amount);
            return a + (Number.isFinite(v) ? Math.round(v * 100) : 0);
          }, 0);
          const left = total - sum;
          const ready = splitDraft.length >= 2 && left === 0
            && splitDraft.every((p) => p.category && Math.round(Number(p.amount) * 100) > 0)
            && new Set(splitDraft.map((p) => p.category)).size === splitDraft.length;
          const sign = txn.amount < 0 ? -1 : 1;
          const set = (i: number, patch: Partial<{ category: string; amount: string }>) =>
            setSplitDraft((d) => d ? d.map((p, j) => j === i ? { ...p, ...patch } : p) : d);
          return (
            <View style={{ marginTop: 10, gap: 8 }}>
              <Text style={s.mut}>
                The parts must add up to {money(Math.abs(txn.amount))}.
              </Text>
              {splitDraft.map((p, i) => (
                <View key={i} style={s.actRow}>
                  <Pressable style={[s.entChip, { flex: 1 }]}
                             onPress={() => { setFilter(""); setPickFor(i); }}
                             accessibilityLabel={`category of part ${i + 1}`}>
                    <Text style={{ color: p.category ? C.text : C.mut, fontSize: 14 }}
                          numberOfLines={1}>
                      {p.category ? catLabel(p.category) : "category…"}
                    </Text>
                  </Pressable>
                  <TextInput style={[s.noteInput, { flex: 0, width: 96, minHeight: 36,
                                                    textAlign: "right" }]}
                             keyboardType="decimal-pad" placeholder="0.00"
                             placeholderTextColor={C.mut} value={p.amount}
                             accessibilityLabel={`amount of part ${i + 1}`}
                             onChangeText={(v) => set(i, { amount: v })} />
                  {left !== 0 ? (
                    <Text style={{ color: C.accent, fontSize: 12 }}
                          onPress={() => set(i, { amount: (Math.max(0,
                            Math.round((Number(p.amount) || 0) * 100) + left) / 100).toFixed(2) })}>
                      ← {(left / 100).toFixed(2)}
                    </Text>
                  ) : null}
                  {splitDraft.length > 2 ? (
                    <Text style={{ color: C.mut, fontSize: 16, padding: 4 }}
                          accessibilityLabel={`remove part ${i + 1}`}
                          onPress={() => setSplitDraft((d) =>
                            d ? d.filter((_, j) => j !== i) : d)}>✕</Text>
                  ) : null}
                </View>
              ))}
              <View style={[s.actRow, { flexWrap: "wrap" }]}>
                <Text style={{ color: C.accent, fontSize: 13 }}
                      onPress={() => setSplitDraft((d) =>
                        d ? [...d, { category: "", amount: "" }] : d)}>
                  + part
                </Text>
                <Text style={{ color: left === 0 ? C.mut : C.warn, fontSize: 12, flex: 1 }}>
                  {left === 0 ? "adds up" : left > 0
                    ? `${(left / 100).toFixed(2)} left to place`
                    : `${(-left / 100).toFixed(2)} over the charge`}
                </Text>
                <Pressable style={[s.btn, s.btnQuiet]}
                           onPress={() => setSplitDraft(null)}>
                  <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
                </Pressable>
                <Pressable style={[s.btn, !ready && { opacity: 0.4 }]}
                           disabled={!ready || splitSave.isPending}
                           onPress={() => splitSave.mutate(splitDraft.map((p) => ({
                             category: p.category,
                             amount: sign * Math.round(Number(p.amount) * 100) / 100 })))}>
                  <Text style={s.btnText}>
                    {splitSave.isPending ? "Saving…" : "Save split"}
                  </Text>
                </Pressable>
              </View>
            </View>
          );
        })()}
      </Card>
      ) : null}

      <Card>
        <View style={s.actRow}>
          <View style={{ flex: 1 }}>
            <Text style={s.mut}>Business expense</Text>
            <Text style={s.actVal}>
              {txn.biz_flag ? "flagged" : "not flagged"}
            </Text>
          </View>
          {!viewer && (
          <Pressable style={[s.btn, txn.biz_flag && s.btnQuiet]}
                     disabled={biz.isPending}
                     onPress={() => biz.mutate(!txn.biz_flag)}>
            <Text style={[s.btnText, txn.biz_flag && { color: C.mut }]}>
              {txn.biz_flag ? "Unflag" : "Flag"}
            </Text>
          </Pressable>
          )}
        </View>
        {txn.biz_flag && (entities.data?.entities ?? [])
          .some((e) => e.status === "active") && (
          <View style={{ marginTop: 10 }}>
            <Text style={s.mut}>
              Assign to entity{assigned !== undefined ? " — assigned ✓" : ""}
            </Text>
            <View style={{ flexDirection: "row", flexWrap: "wrap",
                           gap: 6, marginTop: 4 }}>
              {/* archived entities take no NEW assignments (the server
                  refuses them too) — the web picker applies the same
                  active-only filter */}
              {[{ id: null as string | null, name: "Personal" },
                ...entities.data!.entities
                  .filter((e) => e.status === "active")].map((e) => (
                <Pressable key={e.id ?? "personal"}
                           style={[s.entChip,
                             assigned === e.id && s.entChipOn]}
                           disabled={assign.isPending}
                           onPress={() => assign.mutate(e.id)}>
                  <Text style={{ color: assigned === e.id
                                   ? C.text : C.mut, fontSize: 12 }}>
                    {e.name}
                  </Text>
                </Pressable>
              ))}
            </View>
            {assigned && (
              <View style={{ flexDirection: "row", gap: 12,
                             marginTop: 6 }}>
                <Text style={{ color: C.accent, fontSize: 12 }}
                      onPress={() => !equity.isPending && equity.mutate({
                        entityId: assigned, mode: "contribute" })}>
                  capitalize (owner contribution)
                </Text>
                <Text style={{ color: C.accent, fontSize: 12 }}
                      onPress={() => !equity.isPending && equity.mutate({
                        entityId: assigned, mode: "reimburse" })}>
                  reimburse from business
                </Text>
              </View>
            )}
          </View>
        )}
        <Text style={[s.mut, { marginTop: 10 }]}>Note</Text>
        <View style={s.actRow}>
          <TextInput
            style={s.noteInput}
            placeholder="add a note…" placeholderTextColor={C.mut}
            value={noteDraft ?? txn.note ?? ""}
            onChangeText={setNoteDraft} multiline />
          {noteDraft !== null && noteDraft !== (txn.note ?? "") && (
            <Pressable style={s.btn} disabled={note.isPending}
                       onPress={() => note.mutate(noteDraft.trim())}>
              <Text style={s.btnText}>Save</Text>
            </Pressable>
          )}
        </View>
        {/* offered only where the row is old enough to be stuck — a hold
            from yesterday is still in flight and dropping it would
            delete a charge that is about to post. Not on the demo: one
            visitor deleting rows from the shared synthetic ledger
            degrades it for the next one, and it cannot be undone. */}
        {!viewer && !demo && stuckPending(txn) && (
          <View style={[s.actRow, { marginTop: 10 }]}>
            <View style={{ flex: 1 }}>
              <Text style={s.mut}>Stuck pending</Text>
              <Text style={s.mut}>
                the bank has left this authorization pending for over two
                weeks
              </Text>
            </View>
            <Pressable style={[s.btn, s.btnQuiet]} disabled={retire.isPending}
                       onPress={() => retire.mutate()}>
              <Text style={[s.btnText, { color: C.mut }]}>
                Remove stuck pending
              </Text>
            </Pressable>
          </View>
        )}
      </Card>

      <Card>
        <H>Receipts</H>
        {(receipts.data?.receipts ?? []).map((r) => (
          <View key={r.id} style={{ marginTop: 6 }}>
            <View style={s.actRow}>
              <View style={{ flex: 1 }}>
                <Text style={s.actVal}>
                  {r.status === "parsed" && r.parsed
                    ? `${r.parsed.merchant ?? r.parsed.payee ?? "parsed"}${
                        r.parsed.total != null
                          ? ` · ${money(r.parsed.total)}` : ""}`
                    : r.status}
                </Text>
                <Text style={s.mut}>{mmddyy(r.created_at)} · {r.kind}</Text>
              </View>
              {!viewer && (
                <Text style={[{ color: C.mut, fontSize: 16, padding: 4 },
                      delReceipt.isPending && delReceipt.variables === r.id
                        && { opacity: 0.4 }]}
                      onPress={() => !delReceipt.isPending
                        && delReceipt.mutate(r.id)}>✕</Text>
              )}
            </View>
            {/* the web ReceiptPanel's depth, row by row */}
            {r.status === "parsing" && (
              <Text style={s.mut}>
                Parsing… safe to navigate away — queued work shows under
                Settings → System health.
              </Text>
            )}
            {r.status === "failed" && r.error ? (
              <Text style={{ color: C.bad, fontSize: 12 }}>{r.error}</Text>
            ) : null}
            {r.kind === "check" && r.status === "parsed" && r.parsed && (
              <Text style={s.mut}>
                {r.parsed.check_number
                  ? `check #${r.parsed.check_number} · ` : ""}
                {r.parsed.payee ? `${r.parsed.payee} · ` : ""}
                {r.parsed.amount != null
                  ? `${money(r.parsed.amount)} · ` : ""}
                {r.parsed.date ? `${r.parsed.date} · ` : ""}
                {r.parsed.memo ? `memo: ${r.parsed.memo} · ` : ""}
                {r.parsed.bank ?? ""}
                {r.amount_mismatch
                  ? "\n⚠ parsed amount differs from this transaction"
                  : ""}
              </Text>
            )}
            {(r.items ?? []).map((it) => (
              <Text key={it.line} style={s.mut}>
                {money(it.amount)}
                {it.qty != null && it.qty !== 1 ? ` · ×${it.qty}` : ""}
                {" · "}{it.description}
                {it.tag ? `  [${it.tag}]` : ""}
              </Text>
            ))}
            <View style={{ flexDirection: "row", gap: 14 }}>
              <Text style={{ color: C.accent, fontSize: 12 }}
                    onPress={() => viewImage(r.id, r.mime,
                                             !!r.has_optimized)}>
                view {r.has_optimized ? "optimized" : "image"}
              </Text>
              {r.has_optimized ? (
                <Text style={{ color: C.accent, fontSize: 12 }}
                      onPress={() => viewImage(r.id, r.mime, false)}>
                  original
                </Text>
              ) : null}
              {!viewer && r.status !== "parsed"
                && r.status !== "parsing" && (
                <Text style={{ color: C.accent, fontSize: 12 }}
                      onPress={() => !parse.isPending && parse.mutate(r.id)}>
                  {parse.isPending && parse.variables === r.id ? "queuing…"
                    : r.status === "failed" ? "retry parse" : "parse now"}
                </Text>
              )}
            </View>
          </View>
        ))}
        <View style={{ flexDirection: "row", gap: 8, marginTop: 8,
                       flexWrap: "wrap" }}>
          <Pressable style={s.btn} disabled={upload.isPending}
                     onPress={() => snap(true)}>
            <Text style={s.btnText}>
              {upload.isPending ? "Uploading…" : "📷 Capture"}
            </Text>
          </Pressable>
          <Pressable style={[s.btn, s.btnQuiet]} disabled={upload.isPending}
                     onPress={() => snap(false)}>
            <Text style={[s.btnText, { color: C.mut }]}>Choose photo</Text>
          </Pressable>
          <Pressable style={[s.btn, s.btnQuiet]} disabled={upload.isPending}
                     onPress={() => snap(true, "check")}>
            <Text style={[s.btnText, { color: C.mut }]}>
              Capture check
            </Text>
          </Pressable>
          {/* a check image rarely starts at this camera: the bank's app
              downloads the front, or a scanner makes a PDF. These two
              bring either in without photographing a screen, which
              parses badly. */}
          <Pressable style={[s.btn, s.btnQuiet]} disabled={upload.isPending}
                     onPress={() => snap(false, "check")}>
            <Text style={[s.btnText, { color: C.mut }]}>
              Choose check photo
            </Text>
          </Pressable>
          <Pressable style={[s.btn, s.btnQuiet]} disabled={upload.isPending}
                     onPress={() => pickPdf()}>
            <Text style={[s.btnText, { color: C.mut }]}>Attach PDF</Text>
          </Pressable>
          <Pressable style={[s.btn, s.btnQuiet]} disabled={upload.isPending}
                     onPress={() => pickPdf("check")}>
            <Text style={[s.btnText, { color: C.mut }]}>
              Attach check PDF
            </Text>
          </Pressable>
        </View>
        <Text style={s.mut}>
          Parsed line items also appear on the Items page and match the
          Transactions search.
        </Text>
      </Card>

      {hist.data && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "center",
                         justifyContent: "space-between" }}>
            <H>All {hist.data.payee}</H>
            <Text style={{ color: C.accent, fontSize: 13 }}
                  onPress={() => router.push({ pathname: "/bill-history",
                    params: { payee: hist.data!.payee } } as never)}>
              full history ›
            </Text>
          </View>
          <KV k="Lifetime"
              v={`${money(hist.data.lifetime.total, false)} · ${
                  hist.data.lifetime.count} txns`} />
          {hist.data.lifetime.first && hist.data.lifetime.last && (
            <KV k="First / last"
                v={`${mmddyy(hist.data.lifetime.first)} – ${
                    mmddyy(hist.data.lifetime.last)}`} tone="mut" />)}
          <KV k="Avg / active month"
              v={money(hist.data.lifetime.avg_monthly_active, false)}
              tone="mut" />
          {hist.data.monthly.slice(-6).reverse().map((m) => (
            <KV key={m.month} k={m.month}
                v={`${money(m.total, false)} · ${m.count}`} tone="mut" />
          ))}
          {hist.data.bill ? (
            <KV k="Recurring bill"
                v={`${money(hist.data.bill.amount)} · ${
                    hist.data.bill.cadence}`} tone="good" />
          ) : hist.data.covered_by ? (
            <KV k="Covered by" v={hist.data.covered_by} tone="mut" />
          ) : viewer
            // the web's gate: only a non-transfer EXPENSE can become a
            // bill — an inflow or a transfer is never offered one
            || txn.amount <= 0
            || catLabel(txn.category || "").includes("TRANSFER")
            ? null : (
            // the history screen's add form: amount/cadence/date all
            // choosable, prefilled from the ledger fit — never a silent
            // hardcoded monthly
            <Pressable style={[s.btn, { alignSelf: "flex-start",
                                        marginTop: 8 }]}
                       onPress={() => router.push({
                         pathname: "/bill-history",
                         params: { payee: txn.payee } } as never)}>
              <Text style={s.btnText}>Make this a recurring bill…</Text>
            </Pressable>
          )}
        </Card>
      )}

      <Modal visible={picking || pickFor !== null} animationType="slide" transparent
             onRequestClose={() => { setPicking(false); setPickFor(null); }}>
        <View style={s.sheetWrap}>
          <View style={s.sheet}>
            <Text style={s.sheetTitle}>Category</Text>
            <TextInput style={s.search} placeholder="filter…"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       value={filter} onChangeText={setFilter} />
            <ScrollView style={{ maxHeight: 420 }}>
              {cats.isPending && <Text style={s.mut}>Loading…</Text>}
              {cats.isError && (
                <Text style={{ color: C.bad, fontSize: 14, paddingVertical: 8 }}
                      onPress={() => cats.refetch()}>
                  Couldn't load categories — tap to retry.
                </Text>
              )}
              {/* clearing an override is inherently this-row-only; a
                  split's part has nothing to reset to */}
              {pickFor === null && (
              <Pressable style={s.catRow}
                         onPress={() => setCat.mutate({
                           category: CLEAR_CATEGORY, scope: "one" })}>
                <Text style={{ color: C.mut, fontSize: 15 }}>
                  ↺ Reset to source category
                </Text>
              </Pressable>
              )}
              {filter.trim().length > 1
                && !catList.some((c) =>
                     c.toLowerCase() === filter.trim().toLowerCase()) && (
                <Pressable style={s.catRow}
                           onPress={() => chooseCategory(filter.trim())}>
                  <Text style={{ color: C.accent, fontSize: 15 }}>
                    ✎ Create “{filter.trim()}”
                  </Text>
                </Pressable>
              )}
              {catList.map((c) => (
                <Pressable key={c} style={s.catRow}
                           onPress={() => chooseCategory(c)}>
                  {/* the row's category arrives in DISPLAY form (spaces),
                      the list holds raw keys — compare as labels */}
                  <Text style={{ color: catLabel(c) === catLabel(txn.category)
                                   ? C.accent : C.text, fontSize: 15 }}>
                    {/* the web shows categories with spaces, never the
                        raw FOOD_AND_DRINK enum — the picker and the row
                        above it must not disagree */}
                    {catLabel(c)}
                  </Text>
                </Pressable>
              ))}
            </ScrollView>
            <Pressable style={[s.btn, s.btnQuiet, { alignSelf: "center" }]}
                       onPress={() => { setPicking(false); setPickFor(null); }}>
              <Text style={[s.btnText, { color: C.mut }]}>Close</Text>
            </Pressable>
          </View>
        </View>
      </Modal>
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  payee: { color: C.text, fontSize: 18, fontWeight: "700" },
  big: { color: C.text, fontSize: 30, fontWeight: "700",
         marginVertical: 8, fontVariant: ["tabular-nums"] },
  mut: { color: C.mut, fontSize: 12 },
  actRow: { flexDirection: "row", alignItems: "center", gap: 10 },
  actVal: { color: C.text, fontSize: 15, fontWeight: "600" },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 16, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  entChip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
             borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  entChipOn: { borderColor: C.accent, backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 14, fontWeight: "600" },
  noteInput: { flex: 1, backgroundColor: C.bg, borderColor: C.border,
               borderWidth: 1, borderRadius: 8, color: C.text,
               fontSize: 14, paddingHorizontal: 10, paddingVertical: 8,
               minHeight: 40 },
  sheetWrap: { flex: 1, justifyContent: "flex-end",
               backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: { backgroundColor: C.card, borderTopLeftRadius: 16,
           borderTopRightRadius: 16, padding: 16, gap: 10 },
  sheetTitle: { color: C.text, fontSize: 16, fontWeight: "700" },
  search: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
            borderRadius: 8, color: C.text, fontSize: 14,
            paddingHorizontal: 10, paddingVertical: 7 },
  catRow: { paddingVertical: 9, borderTopColor: C.border,
            borderTopWidth: StyleSheet.hairlineWidth },
});
