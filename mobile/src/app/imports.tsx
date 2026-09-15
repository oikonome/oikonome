// Import & sync — the web Import page, native: sync-now, file imports
// (bulk analyze → plan review → run), CSV column mapping with live
// preview, tax & income documents (SSA/1040/W-2), and undoable recent
// batches. One intake door: a single tax-named file routes to the tax
// pipeline; everything else goes through the bulk plan.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as DocumentPicker from "expo-document-picker";
import { useEffect, useRef, useState } from "react";
import { Alert, Linking, Modal, Pressable, ScrollView, StyleSheet, Text,
         TextInput, View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import StaleBanner from "../components/stale-banner";
import { Card, H, HelpLink, KV, Pill } from "../components/ui";
import { errText, Batch, ImportOutcome, ImportResult, PickedFile, PlanFile,
         SyncStatus, TaxdocPlan, TaxdocRow } from "../lib/api";
import { patchQuery, removeFromList } from "../lib/cache";
import { useSession } from "../lib/session";
import { useOwner, useViewer } from "../lib/viewer";
import { C, mmddyy } from "../lib/theme";

// A whole-ledger restore runs as a background job on the server — a big
// archive takes minutes, longer than any request should be held open — so
// the screen polls it to the end. Same words the web app uses.
function RestoreProgressLine() {
  const { client } = useSession();
  const q = useQuery({
    queryKey: ["restore-progress"],
    queryFn: () => client!.restoreProgress(),
    enabled: !!client,
    refetchInterval: (query) =>
      query.state.data && query.state.data.state !== "running" ? false : 1500,
  });
  const st = q.data;
  if (!st || st.state === "idle") return null;
  if (st.state === "error")
    return <Text style={{ color: C.bad, fontSize: 12 }}>
      {st.progress.error ?? "the restore failed"}</Text>;
  if (st.state === "done") {
    const r = st.progress.result;
    return <Text style={{ color: C.mut, fontSize: 12 }}>
      Restored {r?.imported ?? 0} transactions.
      {(r?.warnings ?? []).map((w) => ` ${w}.`).join("")}</Text>;
  }
  const p = st.progress ?? {};
  return (
    <Text style={{ color: C.mut, fontSize: 12 }}>
      Restoring your export — this can take a few minutes, and it keeps
      going if you leave this screen.
      {p.table ? ` (${String(p.table).replace(/\.csv$/, "")}` +
        (p.rows ? ` · ${p.rows.toLocaleString()} rows)` : ")") : ""}
    </Text>
  );
}

// single file whose NAME looks like a tax document → the tax pipeline
const TAXDOC_RE = /\b(w-?2|1099|1040|ssa|earnings|transcript)\b/i;

// amount-convention choices, the web's exact labels
const SIGNS: [string, string][] = [
  ["bank", "bank CSV (negative = money out — most banks)"],
  ["plaid", "CSV positive = money out"],
  ["statement", "bank / checking statement (PDF)"],
  ["card", "credit-card statement (PDF — payments are credits)"],
  ["investment", "investment / brokerage statement (PDF or activity CSV)"],
];

/** Bottom-sheet single-select — the app's <select>. */
function OptionSheet({ title, open, options, onPick, onClose }: {
  title: string; open: boolean;
  options: [string, string][];
  onPick: (v: string) => void; onClose: () => void;
}) {
  return (
    <Modal visible={open} animationType="slide" transparent
           onRequestClose={onClose}>
      <View style={s.sheetWrap}>
        <View style={s.sheet}>
          <Text style={s.sheetTitle}>{title}</Text>
          <ScrollView style={{ maxHeight: 420 }}>
            {options.map(([v, l], i) => (
              // index-qualified keys: duplicate column names are common
              // in bank exports and collide otherwise
              <Pressable key={`${i}-${v}`} style={s.optRow}
                         onPress={() => { onPick(v); onClose(); }}>
                <Text style={{ color: C.text, fontSize: 15 }}>{l}</Text>
              </Pressable>
            ))}
          </ScrollView>
          <Pressable style={[s.btn, s.btnQuiet, { alignSelf: "center" }]}
                     onPress={onClose}>
            <Text style={[s.btnText, { color: C.mut }]}>Close</Text>
          </Pressable>
        </View>
      </View>
    </Modal>
  );
}

export default function Imports() {
  const { client } = useSession();
  const viewer = useViewer();
  // restoring a whole export over this household is the owner's — a
  // member imports statements and files
  const owner = useOwner();
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
    enabled: !!client, staleTime: 5 * 60_000 });
  const demo = !!me.data?.demo;
  const status = useQuery({
    queryKey: ["sync-status"],
    queryFn: () => client!.syncStatus(),
    enabled: !!client,
    refetchInterval: (q) =>
      q.state.data?.state === "running" ? 2500 : false,
  });
  const start = useMutation({
    mutationFn: () => client!.syncStart(),
    onSuccess: (r) => {
      // "Syncing…" shows now; the status poll takes over from here
      if (r.started)
        patchQuery<SyncStatus>(qc, ["sync-status"], { state: "running" });
      status.refetch();
    },
  });
  // per-connection outcome after a run — the web Maintenance card names
  // each connection's result instead of a bare "finished"
  const conns = useQuery({ queryKey: ["connections"],
    queryFn: () => client!.connections(), enabled: !!client,
    refetchInterval: () =>
      status.data?.state === "running" ? 4000 : false,
  });
  const batches = useQuery({
    queryKey: ["import-batches"],
    queryFn: () => client!.importBatches(),
    enabled: !!client,
  });
  const st = status.data?.state;
  // a finished sync means fresh numbers — but only on the running→done
  // edge; "done" persists and render-time invalidation loops
  const prevState = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (prevState.current === "running" && st === "done") {
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["transactions"] });
    }
    prevState.current = st;
  }, [st, qc]);
  const refresh = () => {
    for (const k of ["import-batches", "today", "transactions", "accounts"])
      qc.invalidateQueries({ queryKey: [k] });
  };
  // undo is a mutation so a second tap while the rollback runs is
  // ignored (it used to fire a second rollback → "Undo failed") and the
  // batch leaves the list the moment the server confirms
  const undo = useMutation({
    mutationFn: (id: string) => client!.importRollback(id),
    onSuccess: (r, id) => {
      removeFromList<{ batches: Batch[] }, Batch>(
        qc, ["import-batches"], "batches", (b) => b.id === id);
      refresh();
      if (r.warning) Alert.alert("Undone with a note", r.warning);
    },
    onError: (e) => Alert.alert("Undo failed", errText(e)),
  });

  // ---- the one intake door ----
  const [staged, setStaged] = useState<PickedFile[]>([]);
  const [taxFile, setTaxFile] = useState<PickedFile | null>(null);
  const [outcome, setOutcome] = useState<ImportOutcome | null>(null);
  const [mapping, setMapping] =
    useState<ImportOutcome["mapping_needed"]>(null);
  const pick = async () => {
    const res = await DocumentPicker.getDocumentAsync(
      { multiple: true, copyToCacheDirectory: true });
    if (res.canceled || !res.assets.length) return;
    const files = res.assets.map((a) => (
      { uri: a.uri, name: a.name, mime: a.mimeType ?? undefined }));
    setOutcome(null); setMapping(null);
    if (files.length === 1 && TAXDOC_RE.test(files[0].name)) {
      setTaxFile(files[0]); setStaged([]);
    } else {
      setTaxFile(null); setStaged(files);
    }
  };
  // a bulk row whose CSV columns defeated the detector escalates into
  // the single-file mapping flow, carrying the plan's decisions
  const escalate = useMutation({
    mutationFn: ({ f, accountId, sign, newName }:
        { f: PickedFile; accountId: string; sign: string;
          newName: string }) =>
      client!.importFile(f, accountId, sign, newName || undefined),
    onSuccess: (r) => {
      setOutcome(r);
      setMapping(r.mapping_needed);
      if (r.result) refresh();
    },
    // a failed re-submit must land in the red note, not vanish
    onError: (e) => setOutcome(
      { result: { error: errText(e) }, mapping_needed: null }),
  });

  const res = outcome?.result;

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => Promise.all([
                                        batches.refetch(), status.refetch()])} />}>
      <StaleBanner query={batches} />
      <Card>
        <H>Sync</H>
        <Text style={s.mut}>
          {st === "running" ? "Pulling from your connected banks…"
            : st === "error"
              ? `Last sync failed: ${status.data?.progress.error ?? "unknown"}`
              : st === "done" ? "Last sync finished."
                : "Pull from your connected banks now."}
        </Text>
        {!viewer && (
        <View style={{ flexDirection: "row", marginTop: 8 }}>
          <Pressable
            style={[s.btn, (st === "running" || start.isPending)
                     && { opacity: 0.5 }]}
            disabled={st === "running" || start.isPending}
            onPress={() => start.mutate()}>
            <Text style={s.btnText}>
              {st === "running" ? "Syncing…" : "Sync now"}
            </Text>
          </Pressable>
        </View>
        )}
        {start.data && !start.data.started && (
          <Text style={{ color: C.warn, fontSize: 12, marginTop: 6 }}>
            {start.data.reason === "already running"
              ? "A sync is already running."
              : "Could not start a sync just now — try again shortly."}
          </Text>
        )}
        {/* which connection ended how — the web Maintenance card's
            per-connection line, from the same status the dots read */}
        {st === "done" && (conns.data?.connections ?? []).length > 0 && (
          <Text style={{ color: (conns.data!.connections.some(
                           (c) => c.status && c.status !== "ok"))
                           ? C.warn : C.mut,
                         fontSize: 12, marginTop: 6 }}>
            {conns.data!.connections.map((c) =>
              `${c.institution_name ?? c.id}: ${c.status ?? "ok"}`)
              .join(" · ")}
          </Text>
        )}
      </Card>

      {viewer ? (
        <Text style={[s.mut, { textAlign: "center", padding: 12 }]}>
          View-only access — the instance owner manages imports.
        </Text>
      ) : (
        <>
          {demo && (
            <Text style={{ color: C.warn, fontSize: 12,
                           textAlign: "center" }}>
              Not available on the demo instance — synthetic data only.
            </Text>
          )}
          <Card>
            <H>Import files</H>
            <Pressable style={[s.btn, { alignSelf: "flex-start" },
                         (demo || escalate.isPending) && { opacity: 0.5 }]}
                       disabled={demo || escalate.isPending}
                       onPress={pick}>
              <Text style={s.btnText}>
                {escalate.isPending ? "Reading…" : "Choose files"}
              </Text>
            </Pressable>
            <Text style={[s.mut, { marginTop: 8 }]}>
              CSV · OFX/QFX · Quicken QIF · a text-layer PDF statement ·
              Mint / YNAB / Monarch / Copilot / Simplifi exports ·
              {owner ? " an Oikonome export ZIP to restore ·" : ""} W-2,
              1099, 1040 and ssa.gov earnings records.
            </Text>
            {!owner && (
              <Text style={s.mut}>
                Restoring an Oikonome export over this household is the
                account owner's — ask them.
              </Text>
            )}
            <Text style={s.mut}>
              Formats are detected, categories mapped, rows your bank
              connection already covers skipped — and every import is
              undoable.
            </Text>
          </Card>

          {res && (
            <Card>
              {res.error ? (
                <Text style={{ color: C.bad, fontSize: 13 }}>
                  {res.error}
                </Text>
              ) : (
                <>
                  <Text style={{ color: C.good, fontSize: 13,
                                 lineHeight: 19 }}>
                    {res.source ? (
                      <Text style={{ fontWeight: "700" }}>
                        {res.source} —{" "}
                      </Text>
                    ) : null}
                    Imported{" "}
                    <Text style={{ fontWeight: "700" }}>
                      {res.imported ?? 0}
                    </Text>
                    {" transactions"}
                    {res.skipped_duplicates
                      ? ` · skipped ${res.skipped_duplicates} already covered`
                      : ""}.
                  </Text>
                  {res.note ? <Text style={s.mut}>{res.note}</Text> : null}
                  {Object.entries(res.split_accounts ?? {}).map(([n, c]) => (
                    <Text key={n} style={s.mut}>• {n}: {c} transactions</Text>
                  ))}
                  {(res.warnings ?? []).map((w, i) => (
                    <Text key={i} style={{ color: C.warn, fontSize: 12 }}>
                      • {w}
                    </Text>
                  ))}
                </>
              )}
            </Card>
          )}

          {mapping && (
            <MappingCard key={mapping.token} need={mapping}
              onDone={(r) => {
                // A refusal normally keeps the card up so the person can
                // fix the one column that was wrong instead of re-uploading
                // — the server hands back a fresh claim token when
                // correcting is still possible. `expired` says it is not:
                // the upload is gone, and a card that cannot submit must
                // not stay on screen, so close it and let the message send
                // them back to the file picker.
                if (!r.error || r.expired) setMapping(null);
                setOutcome({ result: r, mapping_needed: null });
                refresh(); }} />
          )}

          {/* Unconditional, and NOT gated on having just uploaded: the
              restore is a background job that outlives this screen, and
              the line's own copy promises exactly that. Mounted behind the
              post-upload state, a screen the person navigated away from
              and back to showed nothing at all while the job ran, and the
              only way to learn it was still going was to pick the same
              archive again and be refused. It renders null when idle, so
              there is nothing to gate. Same shape as the web page. */}
          <RestoreProgressLine />

          {staged.length > 0 && (
            <PlanReview files={staged}
              onNeedsMapping={(f, accountId, sign, newName) =>
                escalate.mutate({ f, accountId, sign, newName })}
              onDone={refresh} />
          )}

          <TaxDocsCard file={taxFile} />
        </>
      )}

      <Card>
        <H>Recent imports</H>
        {batches.isPending && <Text style={s.mut}>Loading…</Text>}
        {(batches.data?.batches ?? []).slice(0, 15).map((b) => (
          <View key={b.id} style={{ flexDirection: "row",
                                    alignItems: "center", gap: 8 }}>
            <View style={{ flex: 1 }}>
              {/* source ALWAYS shows (the web's Source pill) — a
                  filename alone hides which importer wrote the rows */}
              <KV k={`${mmddyy(b.created_at)} · ${
                      b.filename ? `${b.filename} · ` : ""}${b.source}`}
                  v={b.row_count !== null ? `${b.row_count} rows` : "—"}
                  tone="mut" />
            </View>
            {!viewer && !demo && (
              <Text style={[{ color: C.accent, fontSize: 12, padding: 4 },
                            undo.isPending && undo.variables === b.id
                              && { opacity: 0.4 }]}
                    onPress={() => !undo.isPending && Alert.alert("Undo import",
                      `Remove all ${b.row_count ?? 0} rows from this import?`
                      + (b.annotations ? ` ${b.annotations} receipt${b.annotations === 1 ? "" : "s"}/note${b.annotations === 1 ? "" : "s"}/pairing${b.annotations === 1 ? "" : "s"} added to these rows since will be lost too.` : ""),
                      [{ text: "Undo", style: "destructive",
                         onPress: () => undo.mutate(b.id) },
                       { text: "Cancel", style: "cancel" }])}>
                {undo.isPending && undo.variables === b.id ? "undoing…" : "undo"}
              </Text>
            )}
          </View>
        ))}
        {(batches.data?.batches.length ?? 0) > 15 && (
          <Text style={s.mut}>
            showing 15 of {batches.data!.batches.length} imports
          </Text>
        )}
        {batches.data && batches.data.batches.length === 0 && (
          <Text style={s.mut}>No file imports yet.</Text>
        )}
      </Card>
      <HelpLink topic="import-history" />
    </ScrollView>
  );
}

// ---- bulk analyze → review → run ----
function PlanReview({ files, onNeedsMapping, onDone }: {
  files: PickedFile[];
  onNeedsMapping: (f: PickedFile, accountId: string, sign: string,
                   newName: string) => void;
  onDone: () => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [plan, setPlan] =
    useState<{ token: string; files: PlanFile[] } | null>(null);
  const [results, setResults] = useState<{ index: number; name: string;
    ok: boolean; imported?: number; error?: string;
    restore_started?: boolean }[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [signFor, setSignFor] = useState<number | null>(null);
  const [destFor, setDestFor] = useState<number | null>(null);
  const accounts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(), enabled: !!client });
  const analyze = useMutation({
    mutationFn: () => client!.bulkAnalyze(files),
    onSuccess: setPlan,
    onError: (e) => setErr(errText(e)),
  });
  const analyzeRef = useRef(analyze.mutate);
  analyzeRef.current = analyze.mutate;
  // file-array identity IS the trigger
  useEffect(() => {
    if (!files.length) return;
    setResults(null); setErr(null);
    analyzeRef.current();
  }, [files]);
  const patch = (i: number, p: Partial<PlanFile>) =>
    setPlan((pl) => pl && ({ ...pl,
      files: pl.files.map((f) => (f.index === i ? { ...f, ...p } : f)) }));
  const run = useMutation({
    // the WHOLE edited plan array is posted back — skip is expressed by
    // action, not by omission
    mutationFn: () => client!.bulkRun(plan!.token, plan!.files),
    onSuccess: (r) => {
      // a CSV whose columns the detector can't read says "map them" —
      // escalate that one through the single-file mapping flow with the
      // plan's already-chosen destination + convention
      const stuck = r.results.find(
        (x) => !x.ok && String(x.error).includes("map them"));
      const decided = stuck
        ? plan!.files.find((f) => f.index === stuck.index) : null;
      if (stuck && decided && files[stuck.index])
        onNeedsMapping(files[stuck.index], decided.account_id ?? "",
                       decided.amount_sign, decided.new_account_name ?? "");
      setResults(r.results);
      setPlan(null);
      // onDone refreshes the ledger keys; the rest is what an import can
      // really have changed — a background restore to watch, the wizard's
      // progress, bills detected from the new rows (and so the calendar's
      // next-due dates), and the reports and lenses the new history feeds
      if (r.results.some((x) => x.restore_started))
        qc.invalidateQueries({ queryKey: ["restore-progress"] });
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["bills"] });
      qc.invalidateQueries({ queryKey: ["calendar"] });
      qc.invalidateQueries({ queryKey: ["report"] });
      qc.invalidateQueries({ queryKey: ["lens"] });
      onDone();
    },
    onError: (e) => setErr(errText(e)),
  });

  if (!files.length && !plan && !results && !analyze.isPending) return null;
  const importable = (plan?.files ?? []).filter(
    (f) => f.action === "import");
  const acctName = (id: string | null) =>
    id === null ? "new account…"
      : accounts.data?.accounts.find((a) => a.id === id)?.name ?? id;
  return (
    <Card>
      {analyze.isPending && (
        <Text style={s.mut}>
          reading {files.length} file{files.length === 1 ? "" : "s"}…
        </Text>
      )}
      {/* an analyze failure has no plan to hang the error on — say it
          here or the card renders empty */}
      {err && !plan ? (
        <Text style={{ color: C.bad, fontSize: 12 }}>{err}</Text>
      ) : null}
      {plan && (
        <>
          <H>What we found</H>
          <Text style={s.mut}>
            Nothing is imported until you press the button — change any
            destination or convention first.
          </Text>
          {plan.files.map((f) => (
            <View key={f.index}
                  style={[s.planRow,
                          f.action === "skip" && { opacity: 0.45 }]}>
              <View style={{ flexDirection: "row", alignItems: "center",
                             gap: 8 }}>
                <Text style={{ color: f.action === "import"
                                 ? C.accent : C.mut, fontSize: 18 }}
                      onPress={() => {
                        if (f.kind === "unsupported" || f.oversized) return;
                        patch(f.index, { action: f.action === "import"
                                           ? "skip" : "import" });
                      }}>
                  {f.action === "import" ? "☑" : "☐"}
                </Text>
                <Text style={{ color: C.text, fontSize: 13, flex: 1,
                               fontWeight: "600" }} numberOfLines={1}>
                  {f.name}
                </Text>
                <Pill text={f.kind} />
              </View>
              {f.kind === "Oikonome export ZIP" ? (
                <Text style={s.mut}>into: whole instance</Text>
              ) : (
                <Text style={s.mut}
                      onPress={() => setDestFor(f.index)}>
                  into:{" "}
                  <Text style={{ color: C.accent }}>
                    {acctName(f.account_id)} ▾
                  </Text>
                </Text>
              )}
              {f.account_id === null
                && f.kind !== "Oikonome export ZIP" && (
                <TextInput style={s.input} placeholder="new account name"
                           placeholderTextColor={C.mut}
                           value={f.new_account_name}
                           onChangeText={(v) =>
                             patch(f.index, { new_account_name: v })} />
              )}
              <Text style={s.mut} onPress={() => setSignFor(f.index)}>
                if amounts look backwards:{" "}
                <Text style={{ color: f.sign_certain ? C.accent : C.warn }}>
                  {SIGNS.find(([v]) => v === f.amount_sign)?.[1]
                    ?? f.amount_sign} ▾
                </Text>
                {!f.sign_certain ? "  ⚠ unsure — verify" : ""}
              </Text>
              {f.note ? <Text style={s.mut}>{f.note}</Text> : null}
            </View>
          ))}
          {err ? (
            <Text style={{ color: C.bad, fontSize: 12 }}>{err}</Text>
          ) : null}
          <Pressable style={[s.btn, { alignSelf: "flex-start",
                       marginTop: 8 },
                       (run.isPending || !importable.length)
                         && { opacity: 0.5 }]}
                     disabled={run.isPending || !importable.length}
                     onPress={() => run.mutate()}>
            <Text style={s.btnText}>
              {run.isPending ? "importing…"
                : `Import ${importable.length} file${
                    importable.length === 1 ? "" : "s"}`}
            </Text>
          </Pressable>
        </>
      )}
      {results && (
        <>
          <Text style={{ color: C.text, fontSize: 14, fontWeight: "700" }}>
            {results.filter((r) => r.ok).length}/{results.length} imported.
          </Text>
          {results.map((r) => (
            <Text key={r.index}
                  style={{ color: r.ok ? C.mut : C.bad, fontSize: 12 }}>
              • {r.name}: {r.ok
                ? (r.restore_started ? "restoring…" : `${r.imported} transactions`)
                : r.error}
            </Text>
          ))}
          <Text style={s.mut}>
            Wrong file? Every import is a batch — undo it under Recent
            imports below.
          </Text>
        </>
      )}
      <OptionSheet title="Amount convention" open={signFor !== null}
        options={SIGNS}
        onPick={(v) => signFor !== null
          && patch(signFor, { amount_sign: v, sign_certain: true })}
        onClose={() => setSignFor(null)} />
      <OptionSheet title="Destination account" open={destFor !== null}
        options={[["__new__", "new account…"],
          ...(accounts.data?.accounts ?? []).map((a) =>
            [a.id, `${a.name}${a.mask ? ` …${a.mask}` : ""}`] as
              [string, string])]}
        onPick={(v) => destFor !== null
          && patch(destFor,
                   { account_id: v === "__new__" ? null : v })}
        onClose={() => setDestFor(null)} />
    </Card>
  );
}

// ---- CSV column mapping with live preview ----
function MappingCard({ need, onDone }: {
  need: NonNullable<ImportOutcome["mapping_needed"]>;
  onDone: (r: ImportResult) => void;
}) {
  const { client } = useSession();
  const [cols, setCols] = useState<Record<string, string>>({
    date_col: "", amount_col: "", name_col: "", merchant_col: "",
    category_col: "", debit_col: "", credit_col: "" });
  const [pickFor, setPickFor] = useState<string | null>(null);
  const [dc, setDc] = useState(false);
  // The claim token can be REPLACED by a refusal. Finishing the import
  // consumes the stashed upload, and the commonest mapping mistake — the
  // wrong date column — is only caught while the rows are being parsed,
  // after that has happened. The server re-stashes the same file and
  // returns a new token; submitting the old one again would only ever
  // answer "upload expired".
  const [token, setToken] = useState(need.token);
  const finish = useMutation({
    mutationFn: () => client!.importMapped(token, cols),
    onSuccess: (r) => {
      if (r.result.token) setToken(r.result.token);
      onDone(r.result);
    },
    onError: (e) => onDone({ error: errText(e) }),
  });
  // either one signed Amount or a one-sided column unlocks import: a file
  // that only carries Withdrawals and no Deposits imports fine, so ONE of
  // the three is enough, matching what the server accepts
  const ready = !!(cols.date_col && cols.name_col
    && (cols.amount_col || cols.debit_col || cols.credit_col));
  const at = (row: string[], col: string) => {
    const i = need.header.indexOf(col);
    return i >= 0 ? (row[i] ?? "—") : "—";
  };
  const FIELDS: [string, string, boolean][] = [
    ["date_col", "Date column", true],
    ["amount_col", "Amount column", false],
    ["name_col", "Description / payee column", true],
    ["merchant_col", "Merchant column (optional)", false],
    ["category_col", "Category column (optional)", false],
  ];
  return (
    <Card>
      <H>Map the columns</H>
      <Text style={s.mut}>
        Couldn&apos;t auto-detect{" "}
        <Text style={{ fontWeight: "700" }}>{need.filename}</Text> — pick
        which column is which:
      </Text>
      {FIELDS.map(([k, label]) => (
        <Text key={k} style={[s.mut, { marginTop: 4 }]}
              onPress={() => setPickFor(k)}>
          {label}:{" "}
          <Text style={{ color: C.accent }}>
            {cols[k] || "— pick —"} ▾
          </Text>
        </Text>
      ))}
      <Text style={{ color: C.accent, fontSize: 12, marginTop: 6 }}
            onPress={() => setDc((x) => !x)}>
        {dc ? "▾" : "▸"} No single Amount column? Map Debit and/or Credit
        instead — either one alone is fine
      </Text>
      {dc && (["debit_col", "credit_col"] as const).map((k) => (
        <Text key={k} style={[s.mut, { marginTop: 4 }]}
              onPress={() => setPickFor(k)}>
          {k === "debit_col" ? "Debit column (money out)"
                             : "Credit column (money in)"}:{" "}
          <Text style={{ color: C.accent }}>
            {cols[k] || "— none —"} ▾
          </Text>
        </Text>
      ))}
      {(need.sample?.length ?? 0) > 0 && (
        <>
          <Text style={[s.mut, { marginTop: 8 }]}>
            Your first rows, read the way you have mapped them:
          </Text>
          {need.sample!.length > 4 && (
            <Text style={s.mut}>
              (first 4 of {need.sample!.length} sample rows)
            </Text>
          )}
          {need.sample!.slice(0, 4).map((row, i) => (
            <Text key={i} style={s.mut} numberOfLines={1}>
              {at(row, cols.date_col)} · {at(row, cols.name_col)} ·{" "}
              {cols.amount_col ? at(row, cols.amount_col)
                : [at(row, cols.debit_col), at(row, cols.credit_col)]
                    .filter((x) => x !== "—").join(" / ") || "—"}
            </Text>
          ))}
        </>
      )}
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 8 },
                   (!ready || finish.isPending) && { opacity: 0.5 }]}
                 disabled={!ready || finish.isPending}
                 onPress={() => finish.mutate()}>
        <Text style={s.btnText}>Import with this mapping</Text>
      </Pressable>
      <OptionSheet title="Which column?" open={pickFor !== null}
        options={[["", pickFor === "debit_col" || pickFor === "credit_col"
                    || pickFor === "amount_col"
                    || pickFor === "merchant_col"
                    || pickFor === "category_col"
                    ? "— none —" : "— pick —"],
          ...need.header.map((h) => [h, h] as [string, string])]}
        onPick={(v) => pickFor
          && setCols((c) => ({ ...c, [pickFor]: v }))}
        onClose={() => setPickFor(null)} />
    </Card>
  );
}

// ---- tax & income documents ----
function TaxDocsCard({ file }: { file: PickedFile | null }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [plan, setPlan] = useState<TaxdocPlan | null>(null);
  const [rows, setRows] = useState<TaxdocRow[]>([]);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);
  const analyze = useMutation({
    mutationFn: (f: PickedFile) => client!.taxdocAnalyze(f),
    onSuccess: (p) => {
      setMsg(null); setPlan(p); setRows(p.rows);
      setPicked(new Set(p.rows.map((_, i) => i)));
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  const analyzeRef = useRef(analyze.mutate);
  analyzeRef.current = analyze.mutate;
  useEffect(() => { if (file) analyzeRef.current(file); }, [file]);
  const commit = useMutation({
    mutationFn: () => client!.taxdocCommit(plan!.token,
      rows.filter((_, i) => picked.has(i))),
    onSuccess: (r) => {
      setPlan(null); setRows([]);
      // the income spine feeds the cash-flow report and the retirement
      // projection; neither would notice for a minute otherwise
      qc.invalidateQueries({ queryKey: ["report", "cashflow"] });
      qc.invalidateQueries({ queryKey: ["retirement"] });
      setMsg({ bad: false,
        text: r.kind === "w2"
          ? `Stored ${r.documents} W-2 document${
              r.documents === 1 ? "" : "s"}.`
          : `Income history: ${r.created} year${
              r.created === 1 ? "" : "s"} added, ${r.updated} updated.` });
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  if (!file && !plan && !msg) return null;
  const fields: [keyof TaxdocRow, string][] = plan?.kind === "ssa"
    ? [["ss_earnings", "SS-taxed"], ["medicare_earnings", "Medicare-taxed"]]
    : [["wages", "Wages"], ["total_income", "Total income"],
       ["agi", "AGI"], ["taxable_income", "Taxable"],
       ["tax_paid", "Total tax"]];
  const setField = (i: number, f: keyof TaxdocRow, v: string) =>
    setRows((rs) => rs.map((r, j) => j === i
      ? { ...r, [f]: v === "" ? null
          : Number(v.replace(/[$,\s]/g, "")) } : r));
  return (
    <Card>
      <H>Tax &amp; income document</H>
      <Text style={s.mut}>
        This builds your lifetime income history — it powers the Cash
        Flow report&apos;s early years and retirement analysis, not the
        ledger. Where to get each document:{" "}
        <Text style={{ color: C.accent }}
              onPress={() => Linking.openURL(
                "https://github.com/oikonome/oikonome/blob/main/docs/"
                + "tax-documents.md")}>
          docs/tax-documents.md
        </Text>
        . Everything stays on this instance.
      </Text>
      {msg && (
        <Text style={{ color: msg.bad ? C.bad : C.good, fontSize: 13 }}
              onPress={() => setMsg(null)}>
          {msg.text}
        </Text>
      )}
      {analyze.isPending && file && (
        <Text style={s.mut}>reading {file.name}…</Text>
      )}
      {plan && plan.kind !== "w2" && (
        <>
          {rows.map((r, i) => (
            <View key={r.year} style={s.planRow}>
              <View style={{ flexDirection: "row", alignItems: "center",
                             gap: 8 }}>
                <Text style={{ color: picked.has(i) ? C.accent : C.mut,
                               fontSize: 18 }}
                      onPress={() => setPicked((p) => {
                        const n = new Set(p);
                        if (n.has(i)) n.delete(i); else n.add(i);
                        return n;
                      })}>
                  {picked.has(i) ? "☑" : "☐"}
                </Text>
                <Text style={{ color: C.text, fontWeight: "700",
                               fontSize: 14 }}>{r.year}</Text>
                <Pill text={r.exists ? "update" : "new"}
                      tone={r.exists ? "warn" : "good"} />
              </View>
              <View style={{ flexDirection: "row", flexWrap: "wrap",
                             gap: 6 }}>
                {fields.map(([f, label]) => (
                  <View key={String(f)} style={{ width: "47%" }}>
                    <Text style={{ color: C.mut, fontSize: 10 }}>
                      {label}
                    </Text>
                    <TextInput style={[s.input, { paddingVertical: 4 }]}
                               keyboardType="decimal-pad"
                               value={r[f] == null ? "" : String(r[f])}
                               onChangeText={(v) => setField(i, f, v)} />
                  </View>
                ))}
              </View>
            </View>
          ))}
          <View style={{ flexDirection: "row", gap: 8, marginTop: 8 }}>
            <Pressable style={[s.btn,
                         (commit.isPending || !picked.size)
                           && { opacity: 0.5 }]}
                       disabled={commit.isPending || !picked.size}
                       onPress={() => commit.mutate()}>
              <Text style={s.btnText}>
                {commit.isPending ? "saving…"
                  : `Save ${picked.size} year${
                      picked.size === 1 ? "" : "s"}`}
              </Text>
            </Pressable>
            <Pressable style={[s.btn, s.btnQuiet]}
                       onPress={() => { setPlan(null); setRows([]); }}>
              <Text style={[s.btnText, { color: C.mut }]}>cancel</Text>
            </Pressable>
          </View>
          <Text style={s.mut}>
            only checked rows are written; existing years merge (this
            document&apos;s columns only)
          </Text>
        </>
      )}
      {plan && plan.kind === "w2" && (
        <>
          {rows.map((r, i) => (
            <Text key={i} style={[s.mut, { fontSize: 13 }]}>
              W-2 ·{" "}
              <Text style={{ color: C.text, fontWeight: "700" }}>
                {r.employer ?? "unknown employer"}
              </Text>
              {" · "}{r.year} · box 1 ${r.box1 ?? "?"}
              {r.ein ? ` · EIN ${r.ein}` : ""}
            </Text>
          ))}
          <View style={{ flexDirection: "row", gap: 8, marginTop: 8 }}>
            <Pressable style={[s.btn, commit.isPending && { opacity: 0.5 }]}
                       disabled={commit.isPending}
                       onPress={() => commit.mutate()}>
              <Text style={s.btnText}>
                {commit.isPending ? "saving…" : "Store W-2"}
              </Text>
            </Pressable>
            <Pressable style={[s.btn, s.btnQuiet]}
                       onPress={() => { setPlan(null); setRows([]); }}>
              <Text style={[s.btnText, { color: C.mut }]}>cancel</Text>
            </Pressable>
          </View>
        </>
      )}
    </Card>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  mut: { color: C.mut, fontSize: 12, lineHeight: 17 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 16, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 14, fontWeight: "600" },
  planRow: { borderTopColor: C.border,
             borderTopWidth: StyleSheet.hairlineWidth,
             paddingVertical: 8, gap: 4 },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 7 },
  sheetWrap: { flex: 1, justifyContent: "flex-end",
               backgroundColor: "rgba(0,0,0,0.55)" },
  sheet: { backgroundColor: C.card, borderTopLeftRadius: 16,
           borderTopRightRadius: 16, padding: 16, gap: 10 },
  sheetTitle: { color: C.text, fontSize: 16, fontWeight: "700" },
  optRow: { paddingVertical: 10, borderTopColor: C.border,
            borderTopWidth: StyleSheet.hairlineWidth },
});
