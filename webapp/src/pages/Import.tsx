import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, errText, type Batch, type TaxdocPlan, type TaxdocRow, mmdd, type ImportOutcome, bulkJobLive, type BulkJob, type BulkJobStatus, type ColumnMappingNeeded, type MappingNeeded, type YnabAccountsNeeded, type YnabDestination } from "../api/client";
import { parseMoneyDraft } from "../moneymath";
import { removeFromList } from "../api/cache";
import DemoLock from "../components/DemoLock";
import { keptRowsNote, keptRowsQuestion } from "../components/RemoveAccountPanel";
import { sharedLock } from "../sharedlogin";
import { isOwner, isViewer } from "../role";
import { useElevate } from "../elevation";
import { useLocation } from "react-router";
import HistoryChoice, { HISTORY_CHOICES, HISTORY_SAVING, useHistorySaving }
  from "../components/HistoryChoice";

// Rows older than the household's history window never reach the ledger,
// so an import that dropped some says how many and where the window is
// widened — otherwise a short count reads as a misread file.
function OlderRowsNote({ n }: { n?: number }) {
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  if (!n || n <= 0) return null;
  const win = HISTORY_CHOICES.find(([v]) =>
    v === (s.data?.history_months ?? null))?.[1];
  return (
    <div style={{ marginTop: ".3rem" }}>
      {n} older row{n === 1 ? "" : "s"} left out
      {win ? ` — the history window is ${win}` : ""}; widen it on the
      Accounts page or in the wizard to bring {n === 1 ? "it" : "them"} in.
    </div>
  );
}

// What an undo says beyond "done": the overlap warning, and the rows it
// kept because the bank feed relies on them. A server that folds the
// kept-rows note into the warning is not repeated.
const undoNote = (r: { warning: string | null; note?: string | null }) => {
  const parts = r.warning ? [r.warning] : [];
  if (r.note && !(r.warning ?? "").includes(r.note)) parts.push(r.note);
  return parts.length ? parts.join(" ") : null;
};

const SIGNS: [string, string][] = [
  ["bank", "bank CSV (negative = money out — most banks)"],
  ["plaid", "CSV positive = money out"],
  ["statement", "bank / checking statement (PDF)"],
  ["card", "credit-card statement (PDF — payments are credits)"],
  ["investment", "investment / brokerage statement (PDF or activity CSV)"],
];

// A person holding a file does not know which upload door it belongs to;
// the app does. Intake is the one drop zone below; the plan review is
// purely the review of what was found, before anything is committed.
// A whole-ledger restore runs as a background job (a large archive can
// take minutes to merge, longer than a reverse proxy in front of an
// instance will hold a response open). This watches it to the
// end, so the page says where the restore is instead of looking hung — and
// so a slow restore is never mistaken for a failed one and uploaded twice.
function RestoreProgressCard({ onDone }: { onDone: () => void }) {
  const [seenDone, setSeenDone] = useState(false);
  const q = useQuery({
    queryKey: ["restore-progress"],
    queryFn: api.restoreProgress,
    refetchInterval: (query) =>
      query.state.data && query.state.data.state !== "running" ? false : 1500,
  });
  const st = q.data;
  useEffect(() => {
    if (st && (st.state === "done" || st.state === "error") && !seenDone) {
      setSeenDone(true); onDone();
    }
  }, [st, seenDone, onDone]);
  if (!st || st.state === "idle") return null;
  if (st.state === "error")
    return <div className="note bad">
      {st.progress.error ?? "the restore failed"}</div>;
  if (st.state === "done") {
    const r = st.progress.result;
    return <div className="note good">
      {r?.source && <b>{r.source} — </b>}
      Restored <b>{r?.imported ?? 0}</b> transactions.
      {(r?.warnings ?? []).length > 0 && (
        <ul style={{ margin: ".4rem 0 0 1.2rem" }}>
          {r!.warnings!.map((w, i) => <li key={i}>{w}</li>)}
        </ul>
      )}
    </div>;
  }
  const p = st.progress ?? {};
  return (
    <div className="note">
      Restoring your export — this can take a few minutes for a large
      archive, and it keeps going even if you leave this page.
      {p.table && <div className="mut" style={{ marginTop: ".3rem" }}>
        {p.table.replace(/\.csv$/, "")}
        {p.rows ? ` · ${p.rows.toLocaleString()} rows` : ""}
        {p.tables_done ? ` · ${p.tables_done} tables done` : ""}
      </div>}
    </div>
  );
}

// A folder import runs as a background job on the server: the submit
// answers at once and this card follows the job file by file until it
// settles. Mounted unconditionally, like the restore card: the job
// outlives the page, and a reload or a second tab picks it up again from
// /jobs/current.
const STOPPED_WHY: Record<string, string> = {
  signed_out: "whoever started it was signed out",
  role_changed: "whoever started it may no longer import",
  household_deleted: "this household is being deleted",
  locked_out: "this account is locked right now",
  read_only: "this account is read-only right now",
  second_factor: "the account needs a second factor first",
  unchecked: "the account's standing could not be checked",
  expired: "it waited too long for the server",
};

function BulkJobCard({ onSettled }: { onSettled: (j: BulkJob) => void }) {
  const qc = useQueryClient();
  // jobs this page watched while they ran: only their settling refreshes
  // the ledger and hands a stuck CSV to the mapping step — an outcome
  // read back on a later visit has nothing left to hand over
  const watched = useRef(new Set<string>());
  const q = useQuery<BulkJobStatus>({
    queryKey: ["bulk-import-job"],
    queryFn: api.bulkJobCurrent,
    refetchInterval: (query) => bulkJobLive(query.state.data) ? 2000 : false,
  });
  const cancel = useMutation({
    mutationFn: (id: string) => api.bulkCancel(id),
    onSuccess: (j) => qc.setQueryData(["bulk-import-job"], j),
  });
  const job = q.data && q.data.state !== "idle" ? q.data as BulkJob : null;
  useEffect(() => {
    if (!job) return;
    if (bulkJobLive(job)) { watched.current.add(job.job_id); return; }
    if (watched.current.delete(job.job_id)) onSettled(job);
  }, [job, onSettled]);
  if (!job) return null;
  const live = bulkJobLive(job);
  const ok = job.files.filter((f) => f.ok).length;
  const shown = job.files.filter((f) => f.state !== "pending");
  const plural = (n: number) => `${n} file${n === 1 ? "" : "s"}`;
  const headline = live
    ? `Importing — ${job.done} of ${plural(job.total)} done`
    : job.state === "cancelled"
      ? `Cancelled — ${ok} of ${plural(job.total)} imported.`
      : job.state === "stopped"
        ? `Stopped — ${STOPPED_WHY[job.stop_reason ?? ""]
            ?? "the import could not go on"}. ${ok} of `
          + `${plural(job.total)} imported; drop the rest again.`
        : job.state === "error"
          ? `The import stopped unexpectedly — ${ok} of `
            + `${plural(job.total)} imported; drop the rest again.`
          : `${ok}/${job.total} imported.`;
  return (
    <div className="card">
      <b>{headline}</b>
      {live && <div className="mut" style={{ marginTop: ".3rem" }}>
        {job.waiting ? "Waiting for the server to pick it up… "
                     : ""}It keeps going even if you leave this page.</div>}
      {shown.length > 0 && (
        <ul style={{ margin: ".4rem 0 0", paddingLeft: "1.2rem" }}>
          {shown.map((f) => (
            <li key={f.index}
                style={f.ok || f.state === "running"
                  ? undefined : { color: "var(--red)" }}>
              {f.name}: {f.state === "running" ? "importing…"
                : f.ok ? (f.restore_started ? "restoring…"
                          : `${f.imported ?? 0} transactions`
                            + (f.corrected
                              ? ` · corrected ${f.corrected} an earlier `
                                + "import read differently (sign or year)"
                              : ""))
                  : f.error}
              {f.ok && <OlderRowsNote n={f.skipped_older} />}
              {f.ok && (f.warnings ?? []).length > 0 && (
                <ul style={{ margin: ".2rem 0 0", paddingLeft: "1.2rem" }}>
                  {f.warnings!.map((w, i) => <li key={i}>{w}</li>)}
                </ul>
              )}
            </li>
          ))}
        </ul>
      )}
      {live && (
        <button style={{ marginTop: ".6rem" }}
          disabled={cancel.isPending || job.state === "cancelling"}
          onClick={() => cancel.mutate(job.job_id)}>
          {job.state === "cancelling" ? "stopping after this file…"
            : "Cancel the rest"}</button>
      )}
      {cancel.isError && <p style={{ color: "var(--red)" }}>
        {errText(cancel.error)}</p>}
      {!live && <p className="mut">Wrong file? Every import is a batch —
        undo it under Recent imports below.</p>}
    </div>
  );
}

// An export ZIP the analyze step could not read in full (the server then
// names the restore door on its row) still restores — on its own.
const isRestoreRow = (f: { kind: string; oversized?: boolean;
                           restore_door?: string }) =>
  f.kind === "Oikonome export ZIP" && (!!f.oversized || !!f.restore_door);

type BulkPlanFile = Awaited<ReturnType<typeof api.bulkAnalyze>>["files"][number];

function PlanReview({ files, onSubmitted, onTaxDocs, onRestore }: {
  files: File[];
  // the plan as it was submitted, with the job the server made of it
  onSubmitted: (job: BulkJob, decided: BulkPlanFile[]) => void;
  // rows the server recognised as a tax document, or an archive it says
  // belongs on the restore door, are handed back to their own pipelines
  onTaxDocs: (fs: File[]) => void;
  onRestore?: (f: File) => void;
}) {
  const qc = useQueryClient();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const [plan, setPlan] = useState<Awaited<
    ReturnType<typeof api.bulkAnalyze>> | null>(null);
  const [err, setErr] = useState<string | null>(null);
  // an import leaves out rows older than the STORED history window, read
  // when it runs — a history choice still saving waits for its write
  const saving = useHistorySaving();

  const analyze = useMutation({
    mutationFn: (files: File[]) => api.bulkAnalyze(files),
    onSuccess: (p) => {
      setErr(null);
      const tax = p.files.filter((f) => f.kind === "tax document"
                                        && files[f.index]);
      if (tax.length) onTaxDocs(tax.map((f) => files[f.index]));
      // the run takes the whole plan back, so a handed-off row stays in
      // it as a skip rather than being dropped
      const rest = p.files.filter((f) => !tax.includes(f));
      // a batch that is one export ZIP and nothing else carries the
      // restore door at the top: that door takes it, whatever its size
      const big = rest.length === 1 && files[rest[0].index]
        && (!!p.restore_door || isRestoreRow(rest[0]))
        ? files[rest[0].index] : null;
      if (big && onRestore) { setPlan(null); onRestore(big); return; }
      setPlan(rest.length ? { ...p, files: p.files.map((f) => (
        tax.includes(f) ? { ...f, action: "skip" as const } : f)) } : null);
    },
    onError: (e) => {
      // one archive too large for this door: the refusal names the
      // restore door, which streams it to disk under a far larger cap
      if ((e as { restoreDoor?: string }).restoreDoor && files.length === 1
          && onRestore) {
        setPlan(null); onRestore(files[0]); return;
      }
      setErr(errText(e));
    },
  });
  const run = useMutation({
    mutationFn: () => api.bulkRun(plan!.token, plan!.files),
    onSuccess: (job) => {
      // the files import in the background from here: the job card takes
      // over, and the plan's decisions go with it for the mapping step
      qc.setQueryData(["bulk-import-job"], job);
      onSubmitted(job, plan?.files ?? []);
      setPlan(null);
    },
    onError: (e) => setErr(errText(e)),
  });
  // analyze whatever the drop zone handed over, and re-analyze if it changes
  useEffect(() => {
    if (files.length) analyze.mutate(files);
    // analyze is a stable mutation object; files identity IS the trigger
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files]);
  const patch = (i: number, p: Partial<NonNullable<typeof plan>["files"][0]>) =>
    setPlan(plan && { ...plan,
      files: plan.files.map((f) => (f.index === i ? { ...f, ...p } : f)) });

  const importable = plan?.files.filter((f) => f.action === "import") ?? [];
  // once submitted, the job card shows the outcome; this one has only an
  // analyze in flight, a plan to review, or an error to say
  if (!plan && !analyze.isPending && !err) return null;
  return (
    <div className="card">
      {analyze.isPending && <p className="mut" style={{ margin: 0 }}>
        reading {files.length} file{files.length === 1 ? "" : "s"}…</p>}
      {plan && <>
        <h2 style={{ marginTop: 0 }}>What we found</h2>
        <p className="sub">Nothing is imported until you press the button —
          change any destination or convention first.</p>
      </>}
      {err && <p style={{ color: "var(--red)" }}>{err}</p>}
      {plan && (
        <>
          <div style={{ overflowX: "auto", marginTop: ".8rem" }}>
            <table style={{ fontSize: 13 }}>
              {/* The amount convention is auto-detected; this column is where you
                  correct it when it looks backwards. */}
              <thead><tr><th></th><th>File</th><th>Type</th>
                <th>Into</th><th>If amounts look backwards</th>
                <th>Note</th></tr></thead>
              <tbody>
                {plan.files.map((f) => (
                  <tr key={f.index} style={f.action === "skip"
                      ? { opacity: .45 } : undefined}>
                    <td><input type="checkbox"
                      checked={f.action === "import"}
                      /* can't import an unsupported or oversized
                         (empty-staged) file — keep the box disabled */
                      disabled={f.kind === "unsupported" || f.oversized}
                      onChange={(e) => patch(f.index,
                        { action: e.target.checked ? "import" : "skip" })} /></td>
                    <td style={{ maxWidth: "16rem", overflow: "hidden",
                                 textOverflow: "ellipsis",
                                 whiteSpace: "nowrap" }}
                        title={f.name}>{f.name}</td>
                    <td>{f.kind}</td>
                    <td>
                      {f.kind === "Oikonome export ZIP" ? <span className="mut">whole instance</span> : (
                        <>
                          <select value={f.account_id ?? "__new__"}
                            onChange={(e) => patch(f.index,
                              e.target.value === "__new__"
                                ? { account_id: null }
                                : { account_id: e.target.value })}>
                            <option value="__new__">new account…</option>
                            {(accounts.data?.accounts ?? []).map((a) => (
                              <option key={a.id} value={a.id}>
                                {a.name}{a.mask ? ` …${a.mask}` : ""}</option>
                            ))}
                          </select>
                          {f.account_id === null && (
                            <input value={f.new_account_name}
                              placeholder="new account name"
                              onChange={(e) => patch(f.index,
                                { new_account_name: e.target.value })}
                              style={{ marginLeft: 4, width: "10rem" }} />
                          )}
                        </>
                      )}
                    </td>
                    <td>
                      <select value={f.amount_sign}
                        style={!f.sign_certain
                          ? { borderColor: "var(--amber)" } : undefined}
                        title={f.sign_certain ? ""
                          : "couldn't auto-detect — check this one"}
                        onChange={(e) => patch(f.index,
                          { amount_sign: e.target.value,
                            sign_certain: true })}>
                        {SIGNS.map(([v, l]) =>
                          <option key={v} value={v}>{l}</option>)}
                      </select>
                      {!f.sign_certain && <span title="unsure — verify"
                        style={{ color: "var(--amber)" }}> ⚠</span>}
                    </td>
                    <td className="mut">{f.note}
                      {isRestoreRow(f) && onRestore && files[f.index] && <>
                        {" "}<button type="button"
                          onClick={() => onRestore(files[f.index])}>
                          restore it on its own</button></>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button className="pri" style={{ marginTop: ".6rem" }}
            disabled={run.isPending || !importable.length || saving}
            title={saving ? HISTORY_SAVING : undefined}
            onClick={() => run.mutate()}>
            {run.isPending ? "starting…"
              : `Import ${importable.length} file${importable.length === 1 ? "" : "s"}`}
          </button>
        </>
      )}
    </div>
  );
}

// One door. It takes anything and works out where it goes: a folder of old
// statements, a single CSV, an export ZIP to restore, or a W-2. The list of
// what it accepts lives INSIDE it — that is exactly the information you want
// at the moment you are looking for where to put the file.
// The name test treats "_" as a separator, as file names do ("W2_2023.pdf",
// "2023_TaxReturn.pdf"). An XML file is always an income record: the
// statement importer reads none, and ssa.gov hands earnings out as XML.
const TAXDOC_RE =
  /(?:^|[^a-z0-9])(w-?2|1099(?:-?[a-z]+)?|1040|ssa|earnings|transcript|tax[-_ ]?return)(?=[^a-z0-9]|$)/i;
const isTaxDoc = (f: File) =>
  TAXDOC_RE.test(f.name) || /\.xml$/i.test(f.name) || /\bxml\b/i.test(f.type);
// A single export ZIP goes straight to the restore door, which streams it
// to disk under the restore cap; the plan's analyze step reads each file
// into memory under the much smaller statement cap.
const isArchive = (f: File) => /\.zip$/i.test(f.name);

function DropZone({ onFiles, busy, mayRestore = true }: {
  onFiles: (files: File[]) => void; busy: boolean; mayRestore?: boolean;
}) {
  // a single file imports straight off the drop, and the import reads the
  // stored history window — so a history choice still saving holds the door
  const saving = useHistorySaving();
  const [over, setOver] = useState(false);
  const folderRef = useRef<HTMLInputElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const take = (list: FileList | null) => {
    const fs = Array.from(list ?? []);
    if (fs.length) onFiles(fs);
  };
  return (
    <div className={"dropzone" + (over ? " over" : "")}
         onDragOver={(e) => { e.preventDefault(); setOver(true); }}
         onDragLeave={() => setOver(false)}
         onDrop={(e) => {
           e.preventDefault(); setOver(false);
           if (!saving) take(e.dataTransfer.files);
         }}>
      <input ref={fileRef} type="file" multiple style={{ display: "none" }}
             onChange={(e) => { take(e.target.files); e.target.value = ""; }} />
      <input ref={folderRef} type="file" multiple
        // @ts-expect-error non-standard but universal folder picker
        webkitdirectory=""
        style={{ display: "none" }}
        onChange={(e) => { take(e.target.files); e.target.value = ""; }} />
      <div style={{ fontSize: "1.05rem", fontWeight: 600 }}>
        {busy ? "reading…" : saving ? HISTORY_SAVING : "Drop files here"}</div>
      <div style={{ display: "flex", gap: ".5rem", justifyContent: "center",
                    flexWrap: "wrap", margin: ".6rem 0" }}>
        <button className="pri" disabled={busy || saving}
                onClick={() => fileRef.current?.click()}>choose files</button>
        <button disabled={busy || saving}
                onClick={() => folderRef.current?.click()}>
          or a whole folder</button>
      </div>
      <div className="sub">
        CSV · OFX/QFX · Quicken QIF · a text-layer PDF statement ·
        Mint / YNAB / Monarch / Copilot / Simplifi exports ·
        {mayRestore
          ? " an Oikonome export ZIP to restore ·"
          : ""} W-2, 1099, 1040 and ssa.gov earnings records.
        {!mayRestore && (
          <div style={{ marginTop: ".25rem" }}>Restoring an Oikonome export
            over this household is the account owner's — ask them.</div>
        )}
        <div style={{ marginTop: ".25rem" }}>Formats are detected, categories
          mapped, rows your bank connection already covers skipped — and
          every import is undoable.</div>
      </div>
    </div>
  );
}

// heading/prelude: the Accounts page embeds this section as "Connect
// accounts and import transactions" with the Account-management card on
// top; the standalone /import deep link keeps its own name and shape.
export default function Import({ heading = "Import transactions",
                                 prelude = null, connect = null,
                                 manual = null }: {
  heading?: string; prelude?: ReactNode;
  // Accounts embeds this section with `connect`: the bank card and the
  // file card then render as two EQUAL columns, because connecting a
  // live feed and uploading history are peer choices, not steps. The
  // standalone /import page passes neither and keeps its single column.
  connect?: ReactNode; manual?: ReactNode;
} = {}) {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const batches = useQuery({ queryKey: ["batches"], queryFn: api.importBatches });
  // the setup wizard embeds this page inside its Connect step, which shows
  // the same history choice right below — once on screen is enough
  const inWizard = useLocation().pathname.endsWith("/welcome");
  // What the one door handed over, and where it routed it.
  const [staged, setStaged] = useState<File[]>([]);
  const [taxDocs, setTaxDocs] = useState<File[]>([]);
  const [mapping, setMapping] = useState<MappingNeeded | null>(null);
  const [outcome, setOutcome] = useState<ImportOutcome | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["batches"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["txns"] });
    qc.invalidateQueries({ queryKey: ["accounts"] });
    qc.invalidateQueries({ queryKey: ["calendar"] });
  };

  // restoring an export is the owner's — and never the shared demonstration
  // login's, which may not point the archive's mail relay and AI endpoints
  // anywhere new, so the drop zone does not offer it one
  const mayRestore = isOwner(me) && !sharedLock(me.data, "grant");

  // Route each drop to its pipeline so the person never has to pick one.
  // A tax/income document is its own pipeline (it builds the
  // income spine, not the ledger); everything else goes through the plan.
  const route = (files: File[]) => {
    setOutcome(null); setMapping(null);
    if (files.length === 1 && isArchive(files[0]) && mayRestore) {
      setStaged([]); setTaxDocs([]); restore.mutate(files[0]);
      return;
    }
    setTaxDocs(files.filter(isTaxDoc));
    setStaged(files.filter((f) => !isTaxDoc(f)));
  };

  // The restore door is step-up gated. Open the window before sending the
  // archive, so a large file is not uploaded once only to be refused.
  const elevate = useElevate();
  const restore = useMutation({
    mutationFn: async (f: File) => {
      const st = await api.elevation().catch(() => null);
      if (st && !st.elevated) await elevate(false);
      return api.importFile(f, "", "bank");
    },
    onSuccess: (r) => {
      setOutcome(r.result?.restore_started ? null : r);
      qc.invalidateQueries({ queryKey: ["restore-progress"] });
      refresh();
    },
    onError: (e) => setOutcome({ result: { error:
      /^413\b/.test(e instanceof Error ? e.message : "")
        ? "This export is larger than the instance accepts — restores take "
          + "up to 500 MB, and a proxy in front of the instance may cap "
          + "uploads lower."
        : errText(e) }, mapping_needed: null }),
  });

  // A CSV the detector can't read needs its columns mapped by hand. The
  // plan already decided its destination and convention, so re-submit it
  // with those rather than asking again.
  const escalate = useMutation({
    mutationFn: ({ f, accountId, sign, newName }: {
      f: File; accountId: string; sign: string; newName: string;
    }) => api.importFile(f, accountId, sign, newName),
    onSuccess: (r) => {
      if (r.mapping_needed) setMapping(r.mapping_needed);
      else { setOutcome(r); refresh(); }
    },
    onError: (e) => setOutcome({ result: { error: errText(e) },
                                 mapping_needed: null }),
  });

  // The folder this page submitted, kept until its job settles: a CSV
  // whose columns the detector could not read comes back "import it alone
  // to map them", and the plan already decided its destination and
  // convention, so it goes straight to the mapping step with those.
  const submitted = useRef<{ jobId: string; decided: BulkPlanFile[];
                             files: File[] } | null>(null);
  const onNeedsMapping = (f: File, accountId: string, sign: string,
                          newName: string) =>
    escalate.mutate({ f, accountId, sign, newName });
  const bulkSettled = (job: BulkJob) => {
    const mine = submitted.current?.jobId === job.job_id
      ? submitted.current : null;
    submitted.current = null;
    // a CSV whose columns need mapping, or a YNAB register of several
    // accounts: both are finished one file at a time, on their own card
    const stuck = job.files.find((x) => !x.ok
      && (String(x.error).includes("map them")
          || YNAB_SEVERAL.test(String(x.error))));
    const decided = stuck && mine?.decided.find((f) => f.index === stuck.index);
    const file = stuck && mine?.files[stuck.index];
    if (decided && file)
      onNeedsMapping(file, decided.account_id ?? "", decided.amount_sign,
                     decided.new_account_name ?? "");
    // an import writes ledger rows and maybe accounts: the things that
    // read them, not every query in the app — but that includes every
    // page derived from the ledger (lenses, reports, bills, merchants,
    // reimbursements, business books, budget snapshots), which a
    // 30s-fresh cache would otherwise keep showing pre-import numbers.
    // Prefix invalidates cost nothing while those pages are unmounted.
    for (const k of ["batches", "today", "txns", "accounts", "calendar",
                     "onboarding", "lens-month", "lens-year", "report",
                     "bills", "merchants", "reimb", "biz",
                     "budget-snapshots"])
      qc.invalidateQueries({ queryKey: [k] });
    if (job.restore_started)
      qc.invalidateQueries({ queryKey: ["restore-progress"] });
  };

  if (accounts.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;

  // importing is a write — the /import deep link rendered the full
  // forms for viewers (Accounts already hides its embedded copy)
  if (isViewer(me))
    return (
      <>
        <h1>Import transactions</h1>
        <p className="sub">View-only access — the instance owner manages
          imports.</p>
      </>
    );

  const res = outcome?.result;

  return (
    <>
      <h1>{heading}</h1>
      {prelude}
      {/* demo = no data doors; disabled-in-place, 403 backstop */}
      <DemoLock on={me.data?.demo}>
      {connect ? (
        // .grid2 collapses to one column under 800px, so the phone gets
        // the same two cards stacked — bank first, which is the one most
        // people want.
        <div className="grid2">
          {connect}
          <div className="card">
            <h2>Import a file</h2>
            <p className="sub" style={{ marginTop: 0 }}>
              History from anywhere — a statement, an export from another
              app, or a whole folder of them.</p>
            <DropZone onFiles={route} busy={escalate.isPending || restore.isPending}
                      mayRestore={mayRestore} />
            {manual}
          </div>
        </div>
      ) : (
        <DropZone onFiles={route} busy={escalate.isPending || restore.isPending}
                  mayRestore={mayRestore} />
      )}
      {/* the household's history reach — the same setting the wizard's
          Connect step writes; a file's rows older than it are left out */}
      {!inWizard && (
        <div className="card">
          <HistoryChoice footnote={false} />
        </div>
      )}

      {res?.error && <div className="note bad">{res.error}</div>}
      {res && !res.error && (
        <div className="note good">
          {res.source && <b>{res.source} — </b>}
          Imported <b>{res.imported}</b> transactions
          {res.skipped_duplicates
            ? ` · skipped ${res.skipped_duplicates} already covered`
            : ""}
          {res.corrected
            ? ` · corrected ${res.corrected} an earlier import read `
              + "differently (sign or year)"
            : ""}.
          <OlderRowsNote n={res.skipped_older} />
          {res.note && <div style={{ marginTop: ".3rem" }}>{res.note}</div>}
          {!!res.kept_annotated && (
            <div style={{ marginTop: ".3rem" }}>
              Kept {res.kept_annotated} earlier row{res.kept_annotated === 1
                ? "" : "s"} on the picked account: you had added notes,
              receipts or other changes to {res.kept_annotated === 1
                ? "it" : "them"}.</div>)}
          {!!res.foreign_left && (
            <div style={{ marginTop: ".3rem" }}>
              Left {res.foreign_left} row{res.foreign_left === 1 ? "" : "s"} on
              the picked account that name one of these YNAB accounts but came
              from another export — undo that import below if they do not
              belong there.</div>)}
          {res.split_accounts && (
            <ul style={{ margin: ".4rem 0 0 1.2rem" }}>
              {Object.entries(res.split_accounts).map(([name, n]) => (
                <li key={name}>{name}: {n as number} transactions</li>
              ))}
            </ul>
          )}
          {(res.warnings ?? []).length > 0 && (
            <ul style={{ margin: ".4rem 0 0 1.2rem" }}>
              {res.warnings!.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          )}
        </div>
      )}

      {/* the mapping step appears ONLY when a file actually needs it */}
      {mapping && mapping.kind === "ynab_accounts" && (
        <YnabAccountsCard key={mapping.token} need={mapping}
                          onDone={(r) => {
                            // the same refusal contract as the column card
                            if (!r.error || r.expired) setMapping(null);
                            setOutcome({ result: r, mapping_needed: null });
                            refresh();
                          }} />
      )}
      {mapping && mapping.kind !== "ynab_accounts" && (
        <MappingCard key={mapping.token} need={mapping}
                     onDone={(r) => {
                       // A refusal normally keeps the card up so the person
                       // can fix the one column that was wrong instead of
                       // re-uploading — the server hands back a fresh claim
                       // token when correcting is still possible. `expired`
                       // says it is not: the upload is gone, and a card that
                       // cannot submit must not stay on screen, so close it
                       // and let the message send them to the drop zone.
                       if (!r.error || r.expired) setMapping(null);
                       setOutcome({ result: r, mapping_needed: null });
                       refresh();
                     }} />
      )}

      {/* Unconditional, and NOT gated on having just uploaded: the
          restore is a background job that outlives this page, and the
          card's own copy promises exactly that. Mounted behind the upload
          state, a reload — or simply walking to another page and back —
          would leave a running restore with nothing on screen at all, and
          the only way to learn it was still going would be to drop the same
          archive again and be refused. The card renders null while the
          job is idle, so there is nothing to gate. */}
      <RestoreProgressCard onDone={refresh} />

      <BulkJobCard onSettled={bulkSettled} />

      <PlanReview files={staged}
                  onSubmitted={(job, decided) => {
                    submitted.current = { jobId: job.job_id, decided,
                                          files: staged };
                    if (job.restore_started)
                      qc.invalidateQueries({ queryKey: ["restore-progress"] });
                    // a plan with nothing left to run (an archive alone)
                    // is settled in the submit's own answer
                    if (!bulkJobLive(job)) bulkSettled(job);
                  }}
                  onTaxDocs={(fs) => setTaxDocs((t) => [...t, ...fs])}
                  onRestore={mayRestore ? (f) => {
                    setStaged([]); restore.mutate(f);
                  } : undefined} />

      {taxDocs.map((f, i) => (
        <TaxDocsCard key={`${i}:${f.name}:${f.size}`} file={f} />
      ))}

      <BatchList rows={batches.data?.batches ?? []} onChanged={refresh} />
      </DemoLock>
    </>
  );
}

// tax & income documents → the lifetime income spine
// (income_annual / income_documents) that Cash Flow coverage and
// retirement analysis read. Analyze → editable preview → commit.
function TaxDocsCard({ file }: { file: File | null }) {
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
    mutationFn: (f: File) => api.taxdocAnalyze(f),
    onSuccess: (p) => {
      setMsg(null); setPlan(p); setRows(p.rows); setDrafts({});
      setPicked(new Set(p.rows.map((_, i) => i)));
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  const commit = useMutation({
    mutationFn: (settled: TaxdocRow[]) => api.taxdocCommit(plan!.token,
      settled.filter((_, i) => picked.has(i))),
    onSuccess: (r) => {
      setPlan(null); setRows([]);
      // the income spine feeds Cash Flow's coverage and the retirement model
      qc.invalidateQueries({ queryKey: ["report", "cashflow"] });
      qc.invalidateQueries({ queryKey: ["retirement"] });
      setMsg({ text: r.kind === "w2"
        ? `Stored ${r.documents} W-2 document${r.documents === 1 ? "" : "s"}.`
        : `Income history: ${r.created} year${r.created === 1 ? "" : "s"} added, ${r.updated} updated.`,
        bad: false });
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  // the one door routed a tax/income document here — read it straight away
  useEffect(() => {
    if (file) analyze.mutate(file);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file]);
  const upd = (i: number, f: string, v: string) =>
    setDrafts({ ...drafts, [`${i}:${f}`]: v });
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
  const FIELDS: [keyof TaxdocRow, string][] =
    plan?.kind === "ssa"
      ? [["ss_earnings", "SS-taxed"], ["medicare_earnings", "Medicare-taxed"]]
      : [["wages", "Wages"], ["total_income", "Total income"],
         ["agi", "AGI"], ["taxable_income", "Taxable"],
         ["tax_paid", "Total tax"]];
  // Its own card only while it has something to say.
  if (!file && !plan && !msg) return null;
  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Tax &amp; income document</h2>
      <p className="mut">This builds your lifetime income history — it powers
        the Cash Flow report's early years and retirement analysis, not the
        ledger. Where to get each:{" "}
        <a href="https://github.com/oikonome/oikonome/blob/main/docs/tax-documents.md"
          target="_blank" rel="noreferrer">the how-to</a>. Everything stays
        on this instance.</p>
      {msg && <div className={"note " + (msg.bad ? "bad" : "good")}
                   onClick={() => setMsg(null)}>{msg.text}</div>}
      {analyze.isPending && <p className="mut">reading {file?.name}…</p>}
      {plan && plan.kind !== "w2" && (
        <>
          <table style={{ marginTop: ".6rem" }}>
            <thead><tr><th />
              <th>Year</th>
              {FIELDS.map(([f, label]) => <th className="num" key={f}>{label}</th>)}
              <th /></tr></thead>
            <tbody>
              {rows.map((r, i) => (
                <tr key={r.year}>
                  <td><input type="checkbox" checked={picked.has(i)}
                    onChange={() => { const n = new Set(picked);
                      if (n.has(i)) n.delete(i); else n.add(i);
                      setPicked(n); }} /></td>
                  <td>{r.year}</td>
                  {FIELDS.map(([f]) => (
                    <td className="num" key={f}>
                      <input size={9}
                        style={{ minHeight: 0, padding: ".1rem .3rem",
                                 textAlign: "right" }}
                        value={drafts[`${i}:${f as string}`]
                               ?? (r[f] == null ? "" : String(r[f]))}
                        onChange={(e) => upd(i, f as string, e.target.value)} />
                    </td>
                  ))}
                  <td><span className={"pill " + (r.exists ? "a" : "g")}>
                    {r.exists ? "update" : "new"}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ display: "flex", gap: ".6rem", marginTop: ".6rem",
                        alignItems: "center" }}>
            <button className="pri" disabled={commit.isPending || !picked.size}
                    onClick={save}>
              {commit.isPending ? "saving…"
                : `Save ${picked.size} year${picked.size === 1 ? "" : "s"}`}</button>
            <button onClick={() => { setPlan(null); setRows([]); }}>cancel</button>
            <span className="sub">only checked rows are written; existing
              years merge (this document's columns only)</span>
          </div>
        </>
      )}
      {plan && plan.kind === "w2" && (
        <div style={{ marginTop: ".6rem" }}>
          {rows.map((r, i) => (
            <p key={i} style={{ margin: ".3rem 0" }}>
              W-2 · <b>{r.employer ?? "unknown employer"}</b> · {r.year} ·
              box 1 ${r.box1 ?? "?"}{r.ein ? <> · EIN {r.ein}</> : null}</p>
          ))}
          <button className="pri" disabled={commit.isPending}
                  onClick={() => commit.mutate(rows)}>
            {commit.isPending ? "saving…" : "Store W-2"}</button>{" "}
          <button onClick={() => { setPlan(null); setRows([]); }}>cancel</button>
        </div>
      )}
    </div>
  );
}

function MappingCard({ need, onDone }: {
  need: ColumnMappingNeeded;
  onDone: (result: NonNullable<ImportOutcome["result"]>) => void;
}) {
  const [cols, setCols] = useState<Record<string, string>>({
    date_col: "", amount_col: "", name_col: "",
    merchant_col: "", category_col: "", debit_col: "", credit_col: "",
  });
  // The claim token can be REPLACED by a refusal. Finishing the import
  // consumes the stashed upload, and the commonest mapping mistake — the
  // wrong date column — is only caught while the rows are being parsed,
  // after that has happened. The server re-stashes the same file and
  // returns a new token; submitting the old one again would only ever
  // answer "upload expired".
  const [token, setToken] = useState(need.token);
  // the import reads the stored history window; a choice still saving waits
  const saving = useHistorySaving();
  const finish = useMutation({
    mutationFn: () => api.importMapped(token, cols),
    onSuccess: (r) => {
      if (r.result.token) setToken(r.result.token);
      onDone(r.result);
    },
    onError: (e) => onDone({ error: errText(e) }),
  });
  // A column is named by its POSITION ("#3"), not its header text:
  // duplicate column names are common in bank exports, and by name the two
  // would be one choice — the preview reading the first while the import
  // read the last. The server reads "#n" as the n-th cell.
  const dup = (h: string) =>
    need.header.indexOf(h) !== need.header.lastIndexOf(h);
  const sel = (key: string, label: string, required: boolean) => (
    <label>{label}
      <select required={required} value={cols[key]}
              onChange={(e) => setCols({ ...cols, [key]: e.target.value })}>
        <option value="">{required ? "— pick —" : "— none —"}</option>
        {need.header.map((h, i) => (
          <option key={i} value={`#${i}`}>
            {h}{dup(h) ? ` (column ${i + 1})` : ""}</option>))}
      </select>
    </label>
  );
  // the engine takes either one signed Amount or one-sided columns (debit =
  // money out). Files that only carry one side — a Withdrawals column and no
  // Deposits — import fine, so ONE of the three columns unlocks the button,
  // matching what the server accepts.
  const ready = cols.date_col && cols.name_col &&
    (cols.amount_col || cols.debit_col || cols.credit_col);
  return (
    <div className="card">
      <h2>Map the columns</h2>
      <p className="mut">Couldn't auto-detect <b>{need.filename}</b> — pick
        which column is which:</p>
      <div className="field-grid">
        {sel("date_col", "Date column", true)}
        {sel("amount_col", "Amount column", false)}
        {sel("name_col", "Description / payee column", true)}
        {sel("merchant_col", "Merchant column (optional)", false)}
        {sel("category_col", "Category column (optional)", false)}
      </div>
      <details style={{ marginTop: ".5rem" }}>
        <summary className="mut" style={{ cursor: "pointer" }}>
          No single Amount column? Map Debit and/or Credit instead — either
          one alone is fine</summary>
        <div className="field-grid" style={{ marginTop: ".4rem" }}>
          {sel("debit_col", "Debit column (money out)", false)}
          {sel("credit_col", "Credit column (money in)", false)}
        </div>
      </details>
      {/* a live preview of the first rows UNDER the chosen
          mapping. Without it you confirmed a column mapping against column
          names alone — and a wrong guess is a whole import to undo. */}
      {(need.sample ?? []).length > 0 && (
        <div style={{ marginTop: ".8rem" }}>
          <div className="sub">Your first rows, read the way you have mapped
            them:</div>
          <table style={{ marginTop: ".35rem" }}>
            <thead><tr><th>Date</th><th>Description</th>
              <th className="num">Amount</th></tr></thead>
            <tbody>
              {need.sample!.map((row, i) => {
                const at = (col: string) => {
                  const j = col.startsWith("#") ? Number(col.slice(1)) : -1;
                  return j >= 0 ? (row[j] ?? "") : "";
                };
                const amt = cols.amount_col ? at(cols.amount_col)
                  : [at(cols.debit_col), at(cols.credit_col)]
                      .filter(Boolean).join(" / ");
                return (
                  <tr key={i}>
                    <td className="mut">{at(cols.date_col) || "—"}</td>
                    <td>{at(cols.name_col) || "—"}</td>
                    <td className="num">{amt || "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <button className="pri" style={{ marginTop: ".7rem" }}
              disabled={!ready || finish.isPending || saving}
              title={saving ? HISTORY_SAVING : undefined}
              onClick={() => finish.mutate()}>
        Import with this mapping
      </button>
    </div>
  );
}

// The refusal a folder import gives a YNAB register of several accounts:
// it is finished on its own, on the card below.
const YNAB_SEVERAL = /covers \d+ YNAB accounts/;

// Every YNAB account answered: an account, a new account with a name, or
// skip — and not everything skipped. The server refuses the same cases.
const ynabAllAnswered = (names: string[], dest: Record<string, string>,
                         newName: Record<string, string>) =>
  names.every((n) => !!dest[n]
              && (dest[n] !== "__new__" || !!(newName[n] ?? "").trim()))
  && names.some((n) => dest[n] && dest[n] !== "skip");

// A YNAB register names several YNAB accounts — a checking account and a
// card in one file. Each goes where the person says: an account here, a
// new one, or nowhere. Nothing is matched by name, because a card's lines
// put on checking count twice.
function YnabAccountsCard({ need, onDone }: {
  need: YnabAccountsNeeded;
  onDone: (result: NonNullable<ImportOutcome["result"]>) => void;
}) {
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  // a correctable refusal re-parks the file under a new token
  const [token, setToken] = useState(need.token);
  // the import reads the stored history window; a choice still saving waits
  const saving = useHistorySaving();
  // "" = not answered yet, "skip", "__new__", or an account id
  const [dest, setDest] = useState<Record<string, string>>({});
  const [newName, setNewName] = useState<Record<string, string>>(() =>
    Object.fromEntries(need.accounts.map((a) => [a.name, a.name])));
  // removing what an earlier import left on the picked account is the
  // default when there is something to remove; the person can keep it
  const [remove, setRemove] = useState<Record<string, boolean>>(() =>
    Object.fromEntries(need.accounts.map((a) => [a.name,
                                                 a.stored_on_pick > 0])));
  const names = need.accounts.map((a) => a.name);
  const pickName = accounts.data?.accounts.find((a) => a.id === need.pick)
    ?.name ?? "the picked account";
  const finish = useMutation({
    mutationFn: () => {
      const answers: Record<string, YnabDestination> = {};
      const removing: Record<string, boolean> = {};
      for (const n of names) {
        answers[n] = dest[n] === "__new__"
          ? { new: (newName[n] ?? "").trim() } : dest[n];
        removing[n] = !!remove[n] && dest[n] !== need.pick;
      }
      return api.importYnabAccounts(token, answers, removing);
    },
    onSuccess: (r) => {
      if (r.result.token) setToken(r.result.token);
      onDone(r.result);
    },
    onError: (e) => onDone({ error: errText(e) }),
  });
  const ready = ynabAllAnswered(names, dest, newName);
  return (
    <div className="card">
      <h2>Where does each YNAB account go?</h2>
      <p className="mut"><b>{need.filename}</b> holds {need.accounts.length}{" "}
        YNAB accounts. Choose an account for each, or skip it — nothing is
        imported until every one has an answer.</p>
      <div style={{ overflowX: "auto" }}>
        <table style={{ fontSize: 13 }}>
          <thead><tr><th>YNAB account</th><th className="num">Rows</th>
            <th>Dates</th><th>Into</th></tr></thead>
          <tbody>
            {need.accounts.map((a) => (
              <tr key={a.name}>
                <td>{a.label}</td>
                <td className="num">{a.rows}</td>
                <td className="mut">{mmdd(a.first_date)}–{mmdd(a.last_date)}</td>
                <td>
                  <select value={dest[a.name] ?? ""}
                    onChange={(e) => setDest({ ...dest,
                                               [a.name]: e.target.value })}>
                    <option value="">— choose —</option>
                    {(accounts.data?.accounts ?? []).map((x) => (
                      <option key={x.id} value={x.id}>
                        {x.name}{x.mask ? ` …${x.mask}` : ""}</option>
                    ))}
                    <option value="__new__">new account…</option>
                    <option value="skip">skip — don't import</option>
                  </select>
                  {dest[a.name] === "__new__" && (
                    <input value={newName[a.name] ?? ""}
                      placeholder="new account name"
                      onChange={(e) => setNewName({ ...newName,
                                                    [a.name]: e.target.value })}
                      style={{ marginLeft: 4, width: "10rem" }} />
                  )}
                  {a.stored_on_pick > 0 && !!dest[a.name]
                    && dest[a.name] !== need.pick && (
                    <label style={{ display: "block", marginTop: ".3rem" }}>
                      <input type="checkbox" checked={!!remove[a.name]}
                        onChange={(e) => setRemove({ ...remove,
                          [a.name]: e.target.checked })} />{" "}
                      remove the {a.stored_on_pick} row
                      {a.stored_on_pick === 1 ? "" : "s"} an earlier import
                      of this file put on {pickName}
                    </label>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {names.some((n) => remove[n] && dest[n] && dest[n] !== need.pick
                  && need.accounts.find((a) => a.name === n)!.stored_on_pick > 0) && (
        <p className="mut" style={{ marginTop: ".5rem" }}>Rows you have added
          notes, receipts or other changes to are kept. Undoing this import
          later does not bring removed rows back.</p>
      )}
      <button className="pri" style={{ marginTop: ".7rem" }}
              disabled={!ready || finish.isPending || saving}
              title={saving ? HISTORY_SAVING : undefined}
              onClick={() => finish.mutate()}>
        Import these accounts
      </button>
    </div>
  );
}

// the owner said no to the kept-rows question: nothing happened, so
// nothing is reported
const UNDO_CANCELLED = "undo cancelled";

function BatchList({ rows, onChanged }: {
  rows: { id: string; created_at: string; filename: string | null;
          source: string; row_count: number | null;
          annotations?: number }[];
  onChanged: () => void;
}) {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const owner = isOwner(me);
  const [warning, setWarning] = useState<string | null>(null);
  const undo = useMutation({
    mutationFn: async (batch_id: string) => {
      try {
        return await api.importRollback(batch_id);
      } catch (e) {
        // rows only a note naming no feed account would keep: the server
        // asks the owner before deleting them (otherwise the undo would
        // strip their batch and leave them for good), and the answer
        // re-sends the undo. Deleting them anyway is the owner's alone.
        const q = owner ? keptRowsQuestion(e) : null;
        if (!q) throw e;
        if (!window.confirm(`${q}\n\nUndo anyway?`))
          throw new Error(UNDO_CANCELLED);
        return await api.importRollback(batch_id, true);
      }
    },
    onSuccess: (r, batch_id) => {
      // the undone batch leaves the table now; the refetch that follows
      // only re-proves what the server has already committed
      removeFromList<{ batches: Batch[] }, Batch>(qc, ["batches"], "batches",
                                                  (b) => b.id === batch_id);
      setWarning(undoNote(r)); onChanged();
    },
    // a refusal (a sync running, a batch already gone) says why in place
    // of a button that silently re-enables
    onError: (e) => {
      if (e instanceof Error && e.message === UNDO_CANCELLED) return;
      // a member's undo that would leave kept rows: the server's note says
      // why, in place, with nothing to override it
      const note = keptRowsNote(e) ?? keptRowsQuestion(e);
      if (note) { setWarning(`Not undone. ${note}`); return; }
      window.dispatchEvent(new CustomEvent("oiko-toast",
        { detail: `Couldn't undo the import — ${errText(e)}` }));
    },
  });
  if (rows.length === 0) return null;
  return (
    <div className="card">
      <h2>Recent imports</h2>
      {warning && <div className="note" onClick={() => setWarning(null)}>{warning}</div>}
      <table>
        <thead>
          <tr><th>When</th><th>File</th><th>Source</th>
              <th className="num">Rows</th><th></th></tr>
        </thead>
        <tbody>
          {rows.map((b) => (
            <tr key={b.id}>
              <td className="mut" style={{ whiteSpace: "nowrap" }}>{mmdd(b.created_at)}</td>
              <td className="mut">{b.filename ?? "·"}</td>
              <td><span className="pill m">{b.source}</span></td>
              <td className="num">{b.row_count}</td>
              <td>
                <button disabled={undo.isPending && undo.variables === b.id}
                        onClick={() => {
                          if (window.confirm(`Remove all ${b.row_count ?? 0} rows from this import?`
                              + (b.annotations ? ` ${b.annotations} receipt${b.annotations === 1 ? "" : "s"}/note${b.annotations === 1 ? "" : "s"}/pairing${b.annotations === 1 ? "" : "s"} added to these rows since will be lost too.` : "")))
                            undo.mutate(b.id);
                        }}>undo</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
