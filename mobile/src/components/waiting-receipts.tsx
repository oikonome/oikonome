// Receipts snapped before their transaction reached the ledger — the web
// client's WaitingReceipts card, row for row. Snap one at the till; it
// parses like any receipt, waits here, and the server pairs it with its
// charge when the charge arrives. What the matcher cannot decide alone —
// two charges that fit equally, a receipt with nothing read off it — is
// offered as candidates to match with one tap. A receipt nothing will read
// (no AI on the instance, or a read that failed) takes its total and date
// typed in, and is matched the moment they are saved.
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import * as DocumentPicker from "expo-document-picker";
import * as ImagePicker from "expo-image-picker";
import { useRouter } from "expo-router";
import { Alert, Image, Pressable, StyleSheet, Text, TextInput, View }
  from "react-native";

import { errText, type WaitingReceipt, type WaitingReceipts as Waiting }
  from "../lib/api";
import { patchList, removeFromList } from "../lib/cache";
import { ymd } from "../lib/dates";
import { receiptDetailsDraft } from "../lib/pure";
import { useSession } from "../lib/session";
import { C, mmddyy, money } from "../lib/theme";
import { useDemo, useViewer } from "../lib/viewer";
import { WAITING_KEY, moreWaiting, receiptDateBounds, useMatchReceipt,
         useMoreWaiting, useReceiptDetails, useSnapReceipt,
         useUnmatchReceipt, useWaitingReceipts } from "../lib/waiting";
import DatePicker, { DateField } from "./date-picker";
import { Card, H } from "./ui";

/** Camera, photo library or a PDF → the file the snap door takes. */
export async function pickReceipt(source: "camera" | "library" | "pdf")
    : Promise<{ uri: string; name?: string; type?: string } | null> {
  if (source === "pdf") {
    const res = await DocumentPicker.getDocumentAsync({
      type: "application/pdf", copyToCacheDirectory: true });
    if (res.canceled || !res.assets[0]) return null;
    return { uri: res.assets[0].uri, name: res.assets[0].name || "receipt.pdf",
             type: "application/pdf" };
  }
  if (source === "camera") {
    const perm = await ImagePicker.requestCameraPermissionsAsync();
    if (!perm.granted) return null;
  }
  const fn = source === "camera" ? ImagePicker.launchCameraAsync
                                 : ImagePicker.launchImageLibraryAsync;
  const res = await fn({ mediaTypes: ["images"], quality: 0.8 });
  if (res.canceled || !res.assets[0]) return null;
  // the picker knows the real type and name; a library PNG or HEIC sent
  // as "receipt.jpg" is refused by the door as a mislabelled file
  const a = res.assets[0];
  return { uri: a.uri, name: a.fileName ?? undefined,
           type: a.mimeType ?? undefined };
}

/** Camera, photo library or PDF — the same three the Receipts card offers,
 *  because a receipt photographed earlier is as good as one taken now. */
export function chooseReceiptSource(): Promise<"camera" | "library" | "pdf" | null> {
  return new Promise((resolve) => Alert.alert(
    "Snap a receipt", "It matches its transaction when the charge lands.",
    [{ text: "Camera", onPress: () => resolve("camera") },
     { text: "Photo library", onPress: () => resolve("library") },
     { text: "PDF", onPress: () => resolve("pdf") },
     { text: "Cancel", style: "cancel", onPress: () => resolve(null) }],
    { cancelable: true, onDismiss: () => resolve(null) }));
}

/** "📷 Receipt" — camera, photos or a PDF, for the Transactions tab. The
 *  waiting list is on another screen, so this button says out loud what
 *  happened: saved (with the way to the list) or refused (with the reason). */
export function SnapReceiptButton({ label = "📷 Receipt" }: { label?: string }) {
  const snap = useSnapReceipt();
  const router = useRouter();
  return (
    <Pressable style={[s.btn, snap.isPending && { opacity: 0.5 }]}
               disabled={snap.isPending}
               accessibilityLabel="snap a receipt before its transaction arrives"
               onPress={async () => {
                 const source = await chooseReceiptSource();
                 if (!source) return;
                 const f = await pickReceipt(source);
                 if (!f) {
                   if (source === "camera")
                     Alert.alert("No photo taken",
                       "Allow the camera in your phone's settings, or pick the receipt from your photo library.");
                   return;
                 }
                 try {
                   await snap.mutateAsync(f);
                   Alert.alert("Receipt saved",
                     "It's being read now and waits under More → Receipts until its charge reaches the ledger.",
                     [{ text: "View receipts", onPress: () => router.push("/items") },
                      { text: "OK" }]);
                 } catch (e) {
                   Alert.alert("Couldn't save the receipt", errText(e));
                 }
               }}>
      <Text style={s.btnText}>{snap.isPending ? "Uploading…" : label}</Text>
    </Pressable>
  );
}

/** "N receipts waiting" — opens the Receipts screen. */
export function WaitingBadge() {
  const router = useRouter();
  const q = useWaitingReceipts();
  const n = q.data?.count ?? 0;
  if (!n) return null;
  return (
    <Pressable style={s.pill} onPress={() => router.push("/items")}>
      <Text style={{ color: C.text, fontSize: 12 }}>
        {n} receipt{n === 1 ? "" : "s"} waiting ›</Text>
    </Pressable>
  );
}

function Thumb({ id, mime, optimized }:
               { id: string; mime: string; optimized: boolean }) {
  const { client } = useSession();
  if (!client || mime === "application/pdf") {
    return <View style={[s.thumb, s.thumbPdf]}>
      <Text style={{ color: C.mut, fontSize: 11 }}>PDF</Text></View>;
  }
  const path = `/api/receipts/${encodeURIComponent(id)}/image`
    + (optimized ? "?variant=optimized" : "");
  return <Image style={s.thumb} resizeMode="cover"
                accessibilityLabel="receipt"
                source={{ uri: client.baseUrl + path,
                          headers: client.authHeader }} />;
}

// the web card's status pill words, one for one
function statusWords(r: WaitingReceipt): string {
  if (r.status === "parsing") return "reading…";
  if (r.status === "failed") return "couldn't read";
  if (r.needs_details) return "needs its total";
  if (r.status === "uploaded") return "not read yet";
  return "waiting for a transaction";
}

/** "Enter the total and date" — the web card's DetailsForm: what the AI
 *  could not read (or there is no AI to read it), typed in. Saving asks
 *  the server to match it at once. */
function DetailsForm({ r, onDone }: { r: WaitingReceipt; onDone?: () => void }) {
  const save = useReceiptDetails();
  const [total, setTotal] = useState(r.total != null ? r.total.toFixed(2) : "");
  const [date, setDate] = useState(r.date ?? ymd(new Date()));
  const [store, setStore] = useState(r.merchant ?? "");
  const [picking, setPicking] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // the server takes a date from a year before the snap to the day after
  // (the web form's bounds); the picker offers exactly that
  const bounds = receiptDateBounds(r.created_at);
  const submit = () => {
    const d = receiptDetailsDraft(total, date, store);
    if (!d.ok) { setErr(d.error); return; }
    if (bounds && (d.body.date < bounds.min || d.body.date > bounds.max)) {
      setErr(d.body.date > bounds.max
        ? "That date is after the receipt was snapped"
        : "That date is more than a year before the receipt was snapped");
      return;
    }
    setErr(null);
    save.mutate({ id: r.id, body: d.body }, { onSuccess: () => onDone?.() });
  };
  return (
    <View style={{ gap: 6, marginTop: 4 }}>
      <Text style={s.mut}>Enter the total and date</Text>
      <View style={{ flexDirection: "row", gap: 6 }}>
        <TextInput style={[s.input, { width: 96 }]} placeholder="total"
                   placeholderTextColor={C.mut} keyboardType="decimal-pad"
                   accessibilityLabel="total"
                   value={total} onChangeText={setTotal} />
        <View style={{ flex: 1 }}>
          <DateField value={date} placeholder="date"
                     onPress={() => setPicking(true)}
                     onClear={() => setDate("")} />
        </View>
      </View>
      <TextInput style={s.input} placeholder="store (optional)"
                 placeholderTextColor={C.mut} maxLength={80}
                 accessibilityLabel="store"
                 value={store} onChangeText={setStore} />
      <View style={{ flexDirection: "row", gap: 8 }}>
        <Pressable style={[s.btn, s.btnSmall, save.isPending && { opacity: 0.5 }]}
                   disabled={save.isPending} onPress={submit}>
          <Text style={s.btnText}>{save.isPending ? "Saving…" : "Save"}</Text>
        </Pressable>
        {onDone && (
          <Pressable style={[s.btn, s.btnSmall, s.btnQuiet]} onPress={onDone}>
            <Text style={[s.btnText, { color: C.mut }]}>Cancel</Text>
          </Pressable>)}
      </View>
      {(err || save.isError) && (
        <Text style={{ color: C.bad, fontSize: 12 }}>
          {err ?? errText(save.error)}</Text>)}
      <DatePicker visible={picking} title="Date on the receipt" value={date}
                  min={bounds?.min} max={bounds?.max ?? ymd(new Date())}
                  onPick={(v) => { setDate(v); setPicking(false); }}
                  onClose={() => setPicking(false)} />
    </View>
  );
}

export default function WaitingReceiptsCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const viewer = useViewer();
  // the demo takes matches and unmatches but refuses uploads, typed-in
  // details and removals — those controls are not offered there
  const demo = useDemo();
  const q = useWaitingReceipts();
  const snap = useSnapReceipt();
  const match = useMatchReceipt();
  const unmatch = useUnmatchReceipt();
  const more = useMoreWaiting();
  const router = useRouter();
  // a receipt whose read total is wrong: its details form, opened by hand
  const [fixing, setFixing] = useState<string | null>(null);
  const del = useMutation({
    mutationFn: (id: string) => client!.receiptDelete(id),
    onSuccess: (_r, id) => {
      removeFromList<Waiting, WaitingReceipt>(qc, WAITING_KEY, "waiting",
        (x) => x.id === id);
      // `count` is every receipt waiting, not the rows shown: one fewer
      qc.setQueryData<Waiting>(WAITING_KEY,
        (d) => d && { ...d, count: Math.max(0, d.count - 1) });
    },
  });
  const parse = useMutation({
    mutationFn: (id: string) => client!.receiptParse(id),
    // flip to reading at once — the poll keys off the cache
    onSuccess: (_r, id) => patchList<Waiting, WaitingReceipt>(
      qc, WAITING_KEY, "waiting", (x) => x.id === id,
      { status: "parsing", error: null }),
  });
  const take = async (source: "camera" | "library" | "pdf") => {
    const f = await pickReceipt(source);
    if (f) snap.mutate(f);
  };
  const d = q.data;
  const err = snap.error ?? match.error ?? unmatch.error ?? del.error
    ?? parse.error ?? more.error;
  return (
    <Card>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <H>Waiting receipts</H>
        {d && d.count > 0 && (
          <View style={s.pill}><Text style={{ color: C.text, fontSize: 12 }}>
            {d.count}</Text></View>)}
      </View>
      <Text style={s.mut}>
        Snap a receipt before the charge shows up — it matches its
        transaction by amount, date and store when the charge reaches the
        ledger. When two charges fit equally, you pick.
      </Text>
      {d && !d.ai_available && (
        <Text style={[s.mut, { marginTop: 4 }]}>
          This instance has no AI endpoint to read receipts — enter the total
          and date yourself, or add one in{" "}
          <Text style={s.link} onPress={() => router.push("/settings")}>
            Settings → AI</Text>.</Text>)}
      {!viewer && !demo && (
        <View style={{ flexDirection: "row", gap: 8, marginTop: 8,
                       flexWrap: "wrap" }}>
          <Pressable style={[s.btn, snap.isPending && { opacity: 0.5 }]}
                     disabled={snap.isPending} onPress={() => take("camera")}>
            <Text style={s.btnText}>
              {snap.isPending ? "Uploading…" : "📷 Receipt"}</Text>
          </Pressable>
          <Pressable style={[s.btn, s.btnQuiet]} disabled={snap.isPending}
                     onPress={() => take("library")}>
            <Text style={[s.btnText, { color: C.mut }]}>Choose photo</Text>
          </Pressable>
          <Pressable style={[s.btn, s.btnQuiet]} disabled={snap.isPending}
                     onPress={() => take("pdf")}>
            <Text style={[s.btnText, { color: C.mut }]}>PDF</Text>
          </Pressable>
        </View>
      )}
      {err ? <Text style={{ color: C.bad, fontSize: 12, marginTop: 6 }}>
        {errText(err)}</Text> : null}
      {q.isPending && <Text style={s.mut}>Loading…</Text>}
      {d && d.waiting.length === 0 && (
        <Text style={[s.mut, { marginTop: 6 }]}>
          Nothing waiting — every receipt has its transaction.</Text>)}
      {(d?.waiting ?? []).map((r) => (
        <View key={r.id} style={s.row}>
          <Thumb id={r.id} mime={r.mime} optimized={r.has_optimized} />
          <View style={{ flex: 1, gap: 2 }}>
            <Text style={{ color: C.text, fontSize: 14, fontWeight: "600" }}
                  numberOfLines={1}>
              {r.merchant ?? "Receipt"}
              {r.total != null ? ` · ${money(r.total)}` : ""}
            </Text>
            <Text style={s.mut}>
              {r.date ? `${mmddyy(r.date)} · ` : ""}{statusWords(r)}</Text>
            {r.status === "failed" && r.error ? (
              <Text style={{ color: C.bad, fontSize: 12 }}>{r.error}</Text>
            ) : null}
            {r.status === "uploaded" && !r.needs_details && (
              <Text style={s.mut}>Queued to be read.</Text>)}
            {r.manual && (
              <Text style={s.mut}>Total and date entered by hand.</Text>)}
            {!viewer && !demo && (r.needs_details || fixing === r.id) && (
              <DetailsForm key={r.id + (r.total ?? "")} r={r}
                onDone={fixing === r.id ? () => setFixing(null) : undefined} />)}
            {r.stale && (
              <Text style={s.mut}>
                No charge has matched in {d?.stale_days ?? 30} days — match
                it by hand, or remove it.</Text>)}
            {r.tie && (
              <Text style={s.mut}>
                More than one charge fits — pick the right one.</Text>)}
            {r.released && (
              <Text style={s.mut}>
                Was matched by hand — that charge was removed. Pick its
                charge again.</Text>)}
            {r.candidates.map((c) => (
              <View key={c.txn_id} style={s.cand}>
                <View style={{ flex: 1 }}>
                  <Text style={{ color: C.text, fontSize: 13 }}
                        numberOfLines={1}>
                    {mmddyy(c.date)} · {money(c.amount)} · {c.payee}</Text>
                  <Text style={{ color: C.mut, fontSize: 11 }}>
                    {[c.pending ? "pending" : "",
                      c.exact ? "exact amount" : ""].filter(Boolean)
                      .join(" · ")}</Text>
                </View>
                {!viewer && (
                  <Pressable style={[s.btn, s.btnSmall,
                                     match.isPending && { opacity: 0.5 }]}
                             disabled={match.isPending}
                             onPress={() => match.mutate(
                               { id: r.id, txnId: c.txn_id })}>
                    <Text style={s.btnText}>Match</Text>
                  </Pressable>)}
              </View>
            ))}
            {!viewer && (
              <View style={{ flexDirection: "row", gap: 14, marginTop: 2 }}>
                {d?.ai_available && r.status !== "parsed"
                  && r.status !== "parsing" && (
                  <Text style={s.link}
                        onPress={() => !parse.isPending && parse.mutate(r.id)}>
                    {r.status === "failed" ? "retry" : "read now"}</Text>)}
                {!demo && r.status === "parsed" && !r.needs_details
                  && fixing !== r.id && (
                  <Text style={s.link} onPress={() => setFixing(r.id)}>
                    fix total</Text>)}
                {!demo && (
                  <Text style={[s.link, { color: C.mut }]}
                        onPress={() => !del.isPending && del.mutate(r.id)}>
                    remove</Text>)}
              </View>)}
          </View>
        </View>
      ))}
      {moreWaiting(d) && (
        <View style={[s.row, { alignItems: "center" }]}>
          <Text style={[s.mut, { flex: 1 }]}>
            Showing {d!.waiting.length} of {d!.count}.</Text>
          <Text style={s.link}
                onPress={() => !more.isPending && more.mutate()}>
            {more.isPending ? "loading…" : "show more"}</Text>
        </View>)}
      {(d?.recent.length ?? 0) > 0 && (
        <>
          <Text style={[s.sub, { marginTop: 12 }]}>Matched recently</Text>
          {d!.recent.map((m) => (
            <View key={m.id} style={s.row}>
              <Thumb id={m.id} mime={m.mime} optimized={m.has_optimized} />
              <View style={{ flex: 1 }}>
                <Text style={{ color: C.text, fontSize: 13 }}
                      numberOfLines={1}>
                  {m.payee} · {money(m.txn_amount)}</Text>
                <Text style={s.mut}>
                  {mmddyy(m.txn_date)} · matched automatically</Text>
              </View>
              {!viewer && (
                <Text style={s.link}
                      onPress={() => !unmatch.isPending
                        && unmatch.mutate(m.id)}>unmatch</Text>)}
            </View>
          ))}
        </>
      )}
    </Card>
  );
}

const s = StyleSheet.create({
  mut: { color: C.mut, fontSize: 12 },
  sub: { color: C.text, fontSize: 13, fontWeight: "700" },
  link: { color: C.accent, fontSize: 12 },
  row: { flexDirection: "row", gap: 10, alignItems: "flex-start",
         borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
         paddingTop: 8, marginTop: 8 },
  cand: { flexDirection: "row", alignItems: "center", gap: 8,
          marginTop: 4 },
  thumb: { width: 48, height: 64, borderRadius: 4, borderColor: C.border,
           borderWidth: 1, backgroundColor: C.bg },
  thumbPdf: { alignItems: "center", justifyContent: "center" },
  pill: { backgroundColor: C.hover, borderRadius: 999,
          paddingHorizontal: 10, paddingVertical: 3 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8 },
  btnSmall: { paddingHorizontal: 10, paddingVertical: 5 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8 },
});
