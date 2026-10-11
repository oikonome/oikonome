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
import { discardPicked, errText, Batch, ColumnMappingNeeded, ImportOutcome,
         ImportResult, MappingNeeded, PickedFile, PlanFile, SyncStatus,
         TaxdocPlan, TaxdocRow, YnabAccountsNeeded, YnabDestination }
  from "../lib/api";
import { BulkJob, BulkJobStatus, bulkJobHeadline, bulkJobLive, bulkJobPoll,
         keptRowsNote, keptRowsQuestion, parseMoneyDraft, YNAB_SEVERAL,
         ynabAllAnswered } from "../lib/pure";
import { patchQuery, removeFromList } from "../lib/cache";
import { useSession } from "../lib/session";
import { useOwner, useSharedLock, useViewer } from "../lib/viewer";
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

// A folder import runs as a background job on the server: the submit
// answers at once and this card follows the job file by file until it
// settles, with a way to cancel the rest. Mounted unconditionally, like
// the restore line: the job outlives the screen, and coming back (or
// another device) finds it again through /jobs/current.
// a folder-import file's warnings and corrected rows, as the job status
// carries them per file
type FileChecks = { warnings?: string[]; corrected?: number };

function BulkJobCard({ onSettled }: { onSettled: (j: BulkJob) => void }) {
  const { client } = useSession();
  const qc = useQueryClient();
  // jobs this screen watched while they ran: only their settling
  // refreshes the ledger and hands a stuck CSV to the mapping step
  const watched = useRef(new Set<string>());
  const q = useQuery<BulkJobStatus>({
    queryKey: ["bulk-import-job"],
    queryFn: () => client!.bulkJob(),
    enabled: !!client,
    refetchInterval: (query) => bulkJobPoll(query.state.data),
  });
  const cancel = useMutation({
    mutationFn: (id: string) => client!.bulkCancel(id),
    onSuccess: (j) => qc.setQueryData(["bulk-import-job"], j),
    onError: (e) => Alert.alert("Could not cancel", errText(e)),
  });
  const job = q.data && q.data.state !== "idle" ? q.data as BulkJob : null;
  useEffect(() => {
    if (!job) return;
    if (bulkJobLive(job)) { watched.current.add(job.job_id); return; }
    if (watched.current.delete(job.job_id)) onSettled(job);
  }, [job, onSettled]);
  if (!job) return null;
  const live = bulkJobLive(job);
  return (
    <Card>
      <Text style={{ color: C.text, fontSize: 14, fontWeight: "700" }}>
        {bulkJobHeadline(job)}
      </Text>
      {live && (
        <Text style={s.mut}>
          {job.waiting ? "Waiting for the server to pick it up… " : ""}
          It keeps going if you leave this screen.
        </Text>
      )}
      {job.files.filter((f) => f.state !== "pending").map((f) => {
        // what the file's import asks the person to check, and the rows
        // it corrected: the same two the single-file outcome shows
        const n = f as typeof f & FileChecks;
        return (
          <View key={f.index}>
            <Text style={{ color: f.ok || f.state === "running" ? C.mut
                                                                : C.bad,
                           fontSize: 12 }}>
              • {f.name}: {f.state === "running" ? "importing…"
                : f.ok ? (f.restore_started ? "restoring…"
                          : `${f.imported ?? 0} transactions`
                            + (n.corrected
                              ? ` · corrected ${n.corrected} an earlier `
                                + "import read differently (sign or year)"
                              : ""))
                  : f.error}
            </Text>
            {f.ok ? (n.warnings ?? []).map((w, i) => (
              <Text key={i} style={{ color: C.warn, fontSize: 12,
                                     marginLeft: 12 }}>
                ◦ {w}
              </Text>
            )) : null}
          </View>
        );
      })}
      {live && (
        <Pressable style={[s.btn, s.btnQuiet,
                           { alignSelf: "flex-start", marginTop: 8 },
                           (cancel.isPending || job.state === "cancelling")
                             && { opacity: 0.5 }]}
                   disabled={cancel.isPending || job.state === "cancelling"}
                   onPress={() => cancel.mutate(job.job_id)}>
          <Text style={[s.btnText, { color: C.text }]}>
            {job.state === "cancelling" ? "stopping after this file…"
              : "Cancel the rest"}
          </Text>
        </Pressable>
      )}
      {!live && (
        <Text style={s.mut}>
          Wrong file? Every import is a batch — undo it under Recent
          imports below.
        </Text>
      )}
    </Card>
  );
}

// A file whose name looks like a tax document goes to the tax pipeline.
// "_" counts as a separator, as file names use it ("W2_2023.pdf",
// "2023_TaxReturn.pdf"); an XML file is always an income record — the
// statement importer reads none, and ssa.gov hands earnings out as XML.
const TAXDOC_RE =
  /(?:^|[^a-z0-9])(w-?2|1099(?:-?[a-z]+)?|1040|ssa|earnings|transcript|tax[-_ ]?return)(?=[^a-z0-9]|$)/i;
const isTaxDoc = (f: PickedFile) => TAXDOC_RE.test(f.name)
  || /\.xml$/i.test(f.name) || /\bxml\b/i.test(f.mime ?? "");
// A single export ZIP goes straight to the restore door, which streams it
// to disk under the restore cap; the plan's analyze step reads each file
// into memory under the much smaller statement cap.
const isArchive = (f: PickedFile) => /\.zip$/i.test(f.name);
// an export ZIP the analyze step could not read in full (the server then
// names the restore door on its row)
const isRestoreRow = (f: PlanFile) => f.kind === "Oikonome export ZIP"
  && (!!f.oversized || !!f.restore_door);

// What an undo says beyond "done": the overlap warning, and the rows it
// kept because the bank feed relies on them. A server that folds the
// kept-rows note into the warning is not repeated.
const undoNote = (r: { warning: string | null; note?: string | null }) => {
  const parts = r.warning ? [r.warning] : [];
  if (r.note && !(r.warning ?? "").includes(r.note)) parts.push(r.note);
  return parts.length ? parts.join(" ") : null;
};

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
  // a restore lands the archive's mail relay and AI endpoints, which the
  // shared demonstration login may not point anywhere new — so the card
  // does not offer it one
  const lockGrant = useSharedLock("grant");
  const mayRestore = owner && !lockGrant;
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
      qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
      qc.invalidateQueries({ queryKey: ["transactions"], refetchType: "none" });
    }
    prevState.current = st;
  }, [st, qc]);
  const refresh = () => {
    for (const k of ["import-batches", "today", "transactions", "accounts"])
      qc.invalidateQueries({ queryKey: [k] });
  };
  // undo is a mutation so a second tap while the rollback runs is
  // ignored rather than firing a second rollback, and the batch leaves
  // the list the moment the server confirms
  const undo = useMutation({
    mutationFn: ({ id, force }: { id: string; force?: boolean }) =>
      client!.importRollback(id, force),
    onSuccess: (r, { id }) => {
      removeFromList<{ batches: Batch[] }, Batch>(
        qc, ["import-batches"], "batches", (b) => b.id === id);
      refresh();
      const note = undoNote(r);
      if (note) Alert.alert("Undone with a note", note);
    },
    onError: (e, { id }) => {
      // rows only a note naming no feed account would keep: the server
      // asks the owner before deleting them, and the answer re-sends the
      // undo. A member is told why and offered nothing — deleting them
      // anyway is the owner's call.
      const q = keptRowsQuestion(e);
      const note = keptRowsNote(e) ?? (owner ? null : q);
      if (note !== null) { Alert.alert("Not undone", note); return; }
      if (q === null) { Alert.alert("Undo failed", errText(e)); return; }
      Alert.alert("Undo this import?", q, [
        { text: "Cancel", style: "cancel" },
        { text: "Undo anyway", style: "destructive",
          onPress: () => undo.mutate({ id, force: true }) },
      ]);
    },
  });

  // ---- the one intake door ----
  const [staged, setStaged] = useState<PickedFile[]>([]);
  const [taxFiles, setTaxFiles] = useState<PickedFile[]>([]);
  const [outcome, setOutcome] = useState<ImportOutcome | null>(null);
  const [mapping, setMapping] = useState<MappingNeeded | null>(null);
  const pick = async () => {
    const res = await DocumentPicker.getDocumentAsync(
      { multiple: true, copyToCacheDirectory: true });
    if (res.canceled || !res.assets.length) return;
    const files = res.assets.map((a) => (
      { uri: a.uri, name: a.name, mime: a.mimeType ?? undefined }));
    // the files this pick replaces (statements, a tax form) leave the
    // picker's cache folder now rather than at sign-out
    for (const f of [...staged, ...taxFiles])
      void discardPicked(f.uri);
    setOutcome(null); setMapping(null);
    if (files.length === 1 && isArchive(files[0]) && mayRestore) {
      setTaxFiles([]); setStaged([]); restore.mutate(files[0]);
      return;
    }
    setTaxFiles(files.filter(isTaxDoc));
    setStaged(files.filter((f) => !isTaxDoc(f)));
  };
  const restore = useMutation({
    mutationFn: (f: PickedFile) => client!.restoreArchive(f),
    onSuccess: (r) => {
      setOutcome(r.result?.restore_started ? null : r);
      qc.invalidateQueries({ queryKey: ["restore-progress"] });
      refresh();
    },
    onError: (e) => setOutcome(
      { result: { error: errText(e) }, mapping_needed: null }),
    // the archive is the whole ledger; the server has read the body by the
    // time it answers, so the picker's copy goes either way
    onSettled: (_r, _e, f) => { void discardPicked(f.uri); },
  });
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

  // The folder this screen submitted, kept until its job settles: a CSV
  // whose columns the detector could not read comes back "import it alone
  // to map them", and the plan already chose its destination and
  // convention, so it escalates into the single-file mapping flow with
  // those.
  const submitted = useRef<{ jobId: string; decided: PlanFile[];
                             files: PickedFile[] } | null>(null);
  const bulkSettled = (job: BulkJob) => {
    const mine = submitted.current?.jobId === job.job_id
      ? submitted.current : null;
    submitted.current = null;
    // a CSV whose columns need mapping, or a YNAB register of several
    // accounts: both are finished one file at a time, on their own card
    const stuck = job.files.find(
      (x) => !x.ok && (String(x.error ?? "").includes("map them")
                       || YNAB_SEVERAL.test(String(x.error ?? ""))));
    const decided = stuck
      ? mine?.decided.find((f) => f.index === stuck.index) : undefined;
    const file = stuck ? mine?.files[stuck.index] : undefined;
    if (decided && file)
      escalate.mutate({ f: file, accountId: decided.account_id ?? "",
                        sign: decided.amount_sign,
                        newName: decided.new_account_name ?? "" });
    // the ledger keys, and what an import can really have changed — a
    // background restore to watch, the wizard's progress, bills detected
    // from the new rows (and so the calendar's next-due dates), and the
    // reports and lenses the new history feeds
    refresh();
    if (job.restore_started)
      qc.invalidateQueries({ queryKey: ["restore-progress"] });
    qc.invalidateQueries({ queryKey: ["onboarding"] });
    qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
    qc.invalidateQueries({ queryKey: ["calendar"], refetchType: "none" });
    qc.invalidateQueries({ queryKey: ["report"] });
    qc.invalidateQueries({ queryKey: ["lens"] });
  };

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
                         (demo || escalate.isPending || restore.isPending)
                           && { opacity: 0.5 }]}
                       disabled={demo || escalate.isPending || restore.isPending}
                       onPress={pick}>
              <Text style={s.btnText}>
                {escalate.isPending || restore.isPending
                  ? "Reading…" : "Choose files"}
              </Text>
            </Pressable>
            <Text style={[s.mut, { marginTop: 8 }]}>
              CSV · OFX/QFX · Quicken QIF · a text-layer PDF statement ·
              Mint / YNAB / Monarch / Copilot / Simplifi exports ·
              {mayRestore ? " an Oikonome export ZIP to restore ·" : ""} W-2,
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
                      : ""}
                    {res.corrected
                      ? ` · corrected ${res.corrected} an earlier import `
                        + "read differently (sign or year)"
                      : ""}.
                  </Text>
                  {res.note ? <Text style={s.mut}>{res.note}</Text> : null}
                  {res.kept_annotated ? (
                    <Text style={s.mut}>
                      Kept {res.kept_annotated} earlier row
                      {res.kept_annotated === 1 ? "" : "s"} on the picked
                      account: you had added notes, receipts or other
                      changes to {res.kept_annotated === 1 ? "it" : "them"}.
                    </Text>
                  ) : null}
                  {res.foreign_left ? (
                    <Text style={s.mut}>
                      Left {res.foreign_left} row
                      {res.foreign_left === 1 ? "" : "s"} on the picked
                      account that name one of these YNAB accounts but came
                      from another export — undo that import below if they
                      do not belong there.
                    </Text>
                  ) : null}
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

          {mapping && mapping.kind === "ynab_accounts" && (
            <YnabAccountsCard key={mapping.token} need={mapping}
              onDone={(r) => {
                // the same refusal contract as the column card below
                if (!r.error || r.expired) setMapping(null);
                setOutcome({ result: r, mapping_needed: null });
                refresh(); }} />
          )}
          {mapping && mapping.kind !== "ynab_accounts" && (
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
              the line's own copy promises exactly that. A person who
              navigates away and back must still see the job running, not
              learn it by picking the same archive again and being refused.
              It renders null when idle, so
              there is nothing to gate. Same shape as the web page. */}
          <RestoreProgressLine />

          <BulkJobCard onSettled={bulkSettled} />

          {staged.length > 0 && (
            <PlanReview files={staged}
              onSubmitted={(job, decided) => {
                submitted.current = { jobId: job.job_id, decided,
                                      files: staged };
                if (job.restore_started)
                  qc.invalidateQueries({ queryKey: ["restore-progress"] });
                // a plan with nothing left to run (an archive alone) is
                // settled in the submit's own answer
                if (!bulkJobLive(job)) bulkSettled(job);
              }}
              onTaxDocs={(fs) => setTaxFiles((t) => [...t, ...fs])}
              onRestore={mayRestore ? (f) => {
                setStaged([]); restore.mutate(f);
              } : undefined} />
          )}

          {taxFiles.map((f, i) => (
            <TaxDocsCard key={`${i}:${f.uri}`} file={f} />
          ))}
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
                            undo.isPending && undo.variables?.id === b.id
                              && { opacity: 0.4 }]}
                    onPress={() => !undo.isPending && Alert.alert("Undo import",
                      `Remove all ${b.row_count ?? 0} rows from this import?`
                      + (b.annotations ? ` ${b.annotations} receipt${b.annotations === 1 ? "" : "s"}/note${b.annotations === 1 ? "" : "s"}/pairing${b.annotations === 1 ? "" : "s"} added to these rows since will be lost too.` : ""),
                      [{ text: "Undo", style: "destructive",
                         onPress: () => undo.mutate({ id: b.id }) },
                       { text: "Cancel", style: "cancel" }])}>
                {undo.isPending && undo.variables?.id === b.id ? "undoing…" : "undo"}
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
function PlanReview({ files, onSubmitted, onTaxDocs, onRestore }: {
  files: PickedFile[];
  // the plan as it was submitted, with the job the server made of it
  onSubmitted: (job: BulkJob, decided: PlanFile[]) => void;
  // rows the server recognised as a tax document, or an archive it says
  // belongs on the restore door, are handed back to their own pipelines
  onTaxDocs: (fs: PickedFile[]) => void;
  onRestore?: (f: PickedFile) => void;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [plan, setPlan] =
    useState<{ token: string; files: PlanFile[] } | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [signFor, setSignFor] = useState<number | null>(null);
  const [destFor, setDestFor] = useState<number | null>(null);
  const accounts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(), enabled: !!client });
  const analyze = useMutation({
    mutationFn: () => client!.bulkAnalyze(files),
    onSuccess: (p) => {
      const tax = p.files.filter((f) => f.kind === "tax document"
                                        && files[f.index]);
      if (tax.length) onTaxDocs(tax.map((f) => files[f.index]));
      const rest = p.files.filter((f) => !tax.includes(f));
      // a batch that is one export ZIP and nothing else carries the
      // restore door at the top: that door takes it, whatever its size
      const big = rest.length === 1 && files[rest[0].index]
        && (!!p.restore_door || isRestoreRow(rest[0]))
        ? files[rest[0].index] : null;
      if (big && onRestore) { setPlan(null); onRestore(big); return; }
      // the run takes the whole plan back, so a handed-off row stays in
      // it as a skip rather than being dropped
      setPlan(rest.length ? { ...p, files: p.files.map((f) => (
        tax.includes(f) ? { ...f, action: "skip" as const } : f)) } : null);
    },
    onError: (e) => {
      // one archive too large for this door: the refusal names the
      // restore door, which streams it to disk under a far larger cap
      const body = (e as { body?: { restore_door?: string } }).body;
      if (body?.restore_door && files.length === 1 && onRestore) {
        setPlan(null); onRestore(files[0]); return;
      }
      setErr(errText(e));
    },
  });
  const analyzeRef = useRef(analyze.mutate);
  analyzeRef.current = analyze.mutate;
  // file-array identity IS the trigger
  useEffect(() => {
    if (!files.length) return;
    setErr(null);
    analyzeRef.current();
  }, [files]);
  const patch = (i: number, p: Partial<PlanFile>) =>
    setPlan((pl) => pl && ({ ...pl,
      files: pl.files.map((f) => (f.index === i ? { ...f, ...p } : f)) }));
  const run = useMutation({
    // the WHOLE edited plan array is posted back — skip is expressed by
    // action, not by omission
    mutationFn: () => client!.bulkRun(plan!.token, plan!.files),
    onSuccess: (job) => {
      // the files import in the background from here: the job card takes
      // over, and the plan's decisions go with it for the mapping step
      qc.setQueryData(["bulk-import-job"], job);
      onSubmitted(job, plan?.files ?? []);
      setPlan(null);
    },
    onError: (e) => setErr(errText(e)),
  });

  // once submitted, the job card shows the outcome; this one has only an
  // analyze in flight, a plan to review, or an error to say
  if (!plan && !analyze.isPending && !err) return null;
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
              {isRestoreRow(f) && onRestore && files[f.index] ? (
                <Text style={{ color: C.accent, fontSize: 12 }}
                      onPress={() => onRestore(files[f.index])}>
                  restore it on its own
                </Text>
              ) : null}
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
              {run.isPending ? "starting…"
                : `Import ${importable.length} file${
                    importable.length === 1 ? "" : "s"}`}
            </Text>
          </Pressable>
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

// ---- a YNAB register of several accounts: where each one goes ----
// A checking account and a card in one file: each goes where the person
// says — an account here, a new one, or nowhere. Nothing is matched by
// name, because a card's lines put on checking count twice. Same card as
// the web Import page.
function YnabAccountsCard({ need, onDone }: {
  need: YnabAccountsNeeded;
  onDone: (r: ImportResult) => void;
}) {
  const { client } = useSession();
  const accounts = useQuery({ queryKey: ["accounts"],
    queryFn: () => client!.accounts(), enabled: !!client });
  // a correctable refusal re-parks the file under a new token
  const [token, setToken] = useState(need.token);
  // unanswered, "skip", "__new__", or an account id
  const [dest, setDest] = useState<Record<string, string>>({});
  const [newName, setNewName] = useState<Record<string, string>>(() =>
    Object.fromEntries(need.accounts.map((a) => [a.name, a.name])));
  // removing what an earlier import left on the picked account is the
  // default when there is something to remove; the person can keep it
  const [remove, setRemove] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(need.accounts.map((a) => [a.name,
                                                 a.stored_on_pick > 0])));
  const [destFor, setDestFor] = useState<string | null>(null);
  const names = need.accounts.map((a) => a.name);
  const nameOf = (id: string) =>
    accounts.data?.accounts.find((a) => a.id === id)?.name ?? id;
  const pickName = need.pick ? nameOf(need.pick) : "the picked account";
  const shown = (v: string | undefined) =>
    !v ? "choose…" : v === "skip" ? "skip — don't import"
      : v === "__new__" ? "new account…" : nameOf(v);
  const finish = useMutation({
    mutationFn: () => {
      const answers: Record<string, YnabDestination> = {};
      const removing: Record<string, boolean> = {};
      for (const n of names) {
        answers[n] = dest[n] === "__new__"
          ? { new: (newName[n] ?? "").trim() } : dest[n];
        removing[n] = !!remove[n] && dest[n] !== need.pick;
      }
      return client!.importYnabAccounts(token, answers, removing);
    },
    onSuccess: (r) => {
      if (r.result.token) setToken(r.result.token);
      onDone(r.result);
    },
    onError: (e) => onDone({ error: errText(e) }),
  });
  const ready = ynabAllAnswered(names, dest, newName);
  const removing = need.accounts.some((a) => a.stored_on_pick > 0
    && remove[a.name] && !!dest[a.name] && dest[a.name] !== need.pick);
  return (
    <Card>
      <H>Where does each YNAB account go?</H>
      <Text style={s.mut}>
        {need.filename} holds {need.accounts.length} YNAB accounts. Choose
        an account for each, or skip it — nothing is imported until every
        one has an answer.
      </Text>
      {need.accounts.map((a) => (
        <View key={a.name} style={s.planRow}>
          <Text style={{ color: C.text, fontSize: 13, fontWeight: "600" }}>
            {a.label}
          </Text>
          <Text style={s.mut}>
            {a.rows} row{a.rows === 1 ? "" : "s"} ·{" "}
            {mmddyy(a.first_date)}–{mmddyy(a.last_date)}
          </Text>
          <Text style={s.mut} onPress={() => setDestFor(a.name)}>
            into:{" "}
            <Text style={{ color: dest[a.name] ? C.accent : C.warn }}>
              {shown(dest[a.name])} ▾
            </Text>
          </Text>
          {dest[a.name] === "__new__" && (
            <TextInput style={s.input} placeholder="new account name"
                       placeholderTextColor={C.mut}
                       value={newName[a.name] ?? ""}
                       onChangeText={(v) =>
                         setNewName({ ...newName, [a.name]: v })} />
          )}
          {a.stored_on_pick > 0 && !!dest[a.name]
            && dest[a.name] !== need.pick && (
            <Text style={s.mut}
                  onPress={() => setRemove({ ...remove,
                                             [a.name]: !remove[a.name] })}>
              <Text style={{ color: C.accent, fontSize: 16 }}>
                {remove[a.name] ? "☑" : "☐"}
              </Text>
              {" "}remove the {a.stored_on_pick} row
              {a.stored_on_pick === 1 ? "" : "s"} an earlier import of this
              file put on {pickName}
            </Text>
          )}
        </View>
      ))}
      {removing && (
        <Text style={s.mut}>
          Rows you have added notes, receipts or other changes to are kept.
          Undoing this import later does not bring removed rows back.
        </Text>
      )}
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 8 },
                   (!ready || finish.isPending) && { opacity: 0.5 }]}
                 disabled={!ready || finish.isPending}
                 onPress={() => finish.mutate()}>
        <Text style={s.btnText}>
          {finish.isPending ? "importing…" : "Import these accounts"}
        </Text>
      </Pressable>
      <OptionSheet title="Into which account" open={destFor !== null}
        options={[
          ...(accounts.data?.accounts ?? []).map((x) =>
            [x.id, `${x.name}${x.mask ? ` …${x.mask}` : ""}`] as
              [string, string]),
          ["__new__", "new account…"],
          ["skip", "skip — don't import"]]}
        onPick={(v) => destFor !== null
          && setDest((d) => ({ ...d, [destFor]: v }))}
        onClose={() => setDestFor(null)} />
    </Card>
  );
}

// ---- CSV column mapping with live preview ----
function MappingCard({ need, onDone }: {
  need: ColumnMappingNeeded;
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
  // A column is named by its POSITION ("#3"), not its header text:
  // duplicate column names are common in bank exports, and by name the two
  // would be one choice — the preview reading the first while the import
  // read the last. The server reads "#n" as the n-th cell. Same as web.
  const colLabel = (i: number) => {
    const h = need.header[i] ?? "";
    const dup = need.header.indexOf(h) !== need.header.lastIndexOf(h);
    return dup ? `${h} (column ${i + 1})` : h;
  };
  const colIndex = (col: string) =>
    col.startsWith("#") ? Number(col.slice(1)) : -1;
  const at = (row: string[], col: string) => {
    const i = colIndex(col);
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
            {cols[k] ? colLabel(colIndex(cols[k])) : "— pick —"} ▾
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
            {cols[k] ? colLabel(colIndex(cols[k])) : "— none —"} ▾
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
          ...need.header.map((_h, i) =>
            [`#${i}`, colLabel(i)] as [string, string])]}
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
  // the TEXT of each edited cell ("row:field"), parsed only at save — a
  // per-keystroke parse erased a trailing "." and turned a leading "-"
  // (a loss year's AGI) into a stuck "NaN"
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);
  const analyze = useMutation({
    mutationFn: (f: PickedFile) => client!.taxdocAnalyze(f),
    onSuccess: (p) => {
      setMsg(null); setPlan(p); setRows(p.rows); setDrafts({});
      setPicked(new Set(p.rows.map((_, i) => i)));
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  const analyzeRef = useRef(analyze.mutate);
  analyzeRef.current = analyze.mutate;
  useEffect(() => { if (file) analyzeRef.current(file); }, [file]);
  const commit = useMutation({
    mutationFn: (settled: TaxdocRow[]) => client!.taxdocCommit(plan!.token,
      settled.filter((_, i) => picked.has(i))),
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
    setDrafts((d) => ({ ...d, [`${i}:${String(f)}`]: v }));
  // settle the typed cells; one that is not a number stops the save and
  // names the year, instead of going out as NaN (sent as null → dropped)
  const save = () => {
    const out = rows.map((r) => ({ ...r }));
    for (const [k, v] of Object.entries(drafts)) {
      const [i, f] = [Number(k.slice(0, k.indexOf(":"))),
                      k.slice(k.indexOf(":") + 1)];
      const n = v.trim() === "" ? null : parseMoneyDraft(v);
      if (n === null && v.trim() !== "") {
        if (picked.has(i)) {
          setMsg({ text: `${out[i].year}: that amount is not a number`,
                   bad: true });
          return;
        }
        continue;
      }
      (out[i] as Record<string, unknown>)[f] = n;
    }
    commit.mutate(out);
  };
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
                               keyboardType="numbers-and-punctuation"
                               value={drafts[`${i}:${String(f)}`]
                                 ?? (r[f] == null ? "" : String(r[f]))}
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
                       onPress={save}>
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
                       onPress={() => commit.mutate(rows)}>
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
