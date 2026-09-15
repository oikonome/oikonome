import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { api, errText, type Batch, type TaxdocPlan, type TaxdocRow, mmdd, type ImportOutcome } from "../api/client";
import { removeFromList } from "../api/cache";
import DemoLock from "../components/DemoLock";
import { isOwner, isViewer } from "../role";

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

function PlanReview({ files, onNeedsMapping }: {
  files: File[];
  onNeedsMapping: (f: File, accountId: string, sign: string,
                   newName: string) => void;
}) {
  const qc = useQueryClient();
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const [plan, setPlan] = useState<Awaited<
    ReturnType<typeof api.bulkAnalyze>> | null>(null);
  const [results, setResults] = useState<Awaited<
    ReturnType<typeof api.bulkRun>>["results"] | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const analyze = useMutation({
    mutationFn: (files: File[]) => api.bulkAnalyze(files),
    onSuccess: (p) => { setPlan(p); setResults(null); setErr(null); },
    onError: (e) => setErr(errText(e)),
  });
  const run = useMutation({
    mutationFn: () => api.bulkRun(plan!.token, plan!.files),
    onSuccess: (r, _v, _c) => {
      // A CSV whose columns the detector can't read comes back "import it
      // alone to map them" — carry it straight into the mapping step.
      const stuck = r.results.find((x) => !x.ok
        && String(x.error).includes("map them"));
      const decided = stuck && plan?.files.find((f) => f.index === stuck.index);
      if (stuck && decided && files[stuck.index]) {
        onNeedsMapping(files[stuck.index], decided.account_id ?? "",
                       decided.amount_sign, decided.new_account_name ?? "");
      }
      setResults(r.results); setPlan(null);
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
    },
    onError: (e) => setErr(errText(e)),
  });
  // analyze whatever the drop zone handed over, and re-analyze if it changes
  useEffect(() => {
    if (files.length) { setResults(null); analyze.mutate(files); }
    // analyze is a stable mutation object; files identity IS the trigger
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [files]);
  const patch = (i: number, p: Partial<NonNullable<typeof plan>["files"][0]>) =>
    setPlan(plan && { ...plan,
      files: plan.files.map((f) => (f.index === i ? { ...f, ...p } : f)) });

  const importable = plan?.files.filter((f) => f.action === "import") ?? [];
  if (!files.length && !plan && !results && !analyze.isPending) return null;
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
              {/* "Amount convention" was jargon on a required-looking
                  select. It is auto-detected; this column is where you
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
                    <td className="mut">{f.note}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <button className="pri" style={{ marginTop: ".6rem" }}
            disabled={run.isPending || !importable.length}
            onClick={() => run.mutate()}>
            {run.isPending ? "importing…"
              : `Import ${importable.length} file${importable.length === 1 ? "" : "s"}`}
          </button>
        </>
      )}
      {results && (
        <div style={{ marginTop: ".8rem" }}>
          <b>{results.filter((r) => r.ok).length}/{results.length} imported.</b>
          <ul style={{ margin: ".4rem 0 0", paddingLeft: "1.2rem" }}>
            {results.map((r) => (
              <li key={r.index} className={r.ok ? "" : ""}
                  style={r.ok ? undefined : { color: "var(--red)" }}>
                {r.name}: {r.ok
                  ? (r.restore_started ? "restoring…" : `${r.imported} transactions`)
                  : r.error}
              </li>
            ))}
          </ul>
          <p className="mut">Wrong file? Every import is a batch — undo it
            under Recent imports below.</p>
        </div>
      )}
    </div>
  );
}

// One door. It takes anything and works out where it goes: a folder of old
// statements, a single CSV, an export ZIP to restore, or a W-2. The list of
// what it accepts lives INSIDE it — that is exactly the information you want
// at the moment you are looking for where to put the file.
const TAXDOC_RE = /\b(w-?2|1099|1040|ssa|earnings|transcript)\b/i;

function DropZone({ onFiles, busy, mayRestore = true }: {
  onFiles: (files: File[]) => void; busy: boolean; mayRestore?: boolean;
}) {
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
           e.preventDefault(); setOver(false); take(e.dataTransfer.files);
         }}>
      <input ref={fileRef} type="file" multiple style={{ display: "none" }}
             onChange={(e) => { take(e.target.files); e.target.value = ""; }} />
      <input ref={folderRef} type="file" multiple
        // @ts-expect-error non-standard but universal folder picker
        webkitdirectory=""
        style={{ display: "none" }}
        onChange={(e) => { take(e.target.files); e.target.value = ""; }} />
      <div style={{ fontSize: "1.05rem", fontWeight: 600 }}>
        {busy ? "reading…" : "Drop files here"}</div>
      <div style={{ display: "flex", gap: ".5rem", justifyContent: "center",
                    flexWrap: "wrap", margin: ".6rem 0" }}>
        <button className="pri" disabled={busy}
                onClick={() => fileRef.current?.click()}>choose files</button>
        <button disabled={busy}
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
  // What the one door handed over, and where it routed it.
  const [staged, setStaged] = useState<File[]>([]);
  const [taxDoc, setTaxDoc] = useState<File | null>(null);
  const [mapping, setMapping] =
    useState<NonNullable<ImportOutcome["mapping_needed"]> | null>(null);
  const [outcome, setOutcome] = useState<ImportOutcome | null>(null);

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["batches"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["txns"] });
    qc.invalidateQueries({ queryKey: ["accounts"] });
    qc.invalidateQueries({ queryKey: ["calendar"] });
  };

  // The routing the person used to have to do themselves, by picking one of
  // three cards. A tax/income document is its own pipeline (it builds the
  // income spine, not the ledger); everything else goes through the plan.
  const route = (files: File[]) => {
    setOutcome(null); setMapping(null);
    if (files.length === 1 && TAXDOC_RE.test(files[0].name)) {
      setStaged([]); setTaxDoc(files[0]);
    } else {
      setTaxDoc(null); setStaged(files);
    }
  };

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
            <DropZone onFiles={route} busy={escalate.isPending}
                      mayRestore={isOwner(me)} />
            {manual}
          </div>
        </div>
      ) : (
        <DropZone onFiles={route} busy={escalate.isPending}
                  mayRestore={isOwner(me)} />
      )}

      {res?.error && <div className="note bad">{res.error}</div>}
      {res && !res.error && (
        <div className="note good">
          {res.source && <b>{res.source} — </b>}
          Imported <b>{res.imported}</b> transactions
          {res.skipped_duplicates
            ? ` · skipped ${res.skipped_duplicates} already covered`
            : ""}.
          {res.note && <div style={{ marginTop: ".3rem" }}>{res.note}</div>}
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
      {mapping && (
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

      <PlanReview files={staged}
                  onNeedsMapping={(f, accountId, sign, newName) =>
                    escalate.mutate({ f, accountId, sign, newName })} />

      <TaxDocsCard file={taxDoc} />

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
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null);
  const analyze = useMutation({
    mutationFn: (f: File) => api.taxdocAnalyze(f),
    onSuccess: (p) => {
      setMsg(null); setPlan(p); setRows(p.rows);
      setPicked(new Set(p.rows.map((_, i) => i)));
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  const commit = useMutation({
    mutationFn: () => api.taxdocCommit(plan!.token,
      rows.filter((_, i) => picked.has(i))),
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
    setRows(rows.map((r, j) => j === i
      ? { ...r, [f]: v === "" ? null : Number(v.replace(/[$,\s]/g, "")) }
      : r));
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
                      <input inputMode="decimal" size={9}
                        style={{ minHeight: 0, padding: ".1rem .3rem",
                                 textAlign: "right" }}
                        value={r[f] == null ? "" : String(r[f])}
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
                    onClick={() => commit.mutate()}>
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
                  onClick={() => commit.mutate()}>
            {commit.isPending ? "saving…" : "Store W-2"}</button>{" "}
          <button onClick={() => { setPlan(null); setRows([]); }}>cancel</button>
        </div>
      )}
    </div>
  );
}

function MappingCard({ need, onDone }: {
  need: NonNullable<ImportOutcome["mapping_needed"]>;
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
  const finish = useMutation({
    mutationFn: () => api.importMapped(token, cols),
    onSuccess: (r) => {
      if (r.result.token) setToken(r.result.token);
      onDone(r.result);
    },
    onError: (e) => onDone({ error: errText(e) }),
  });
  const sel = (key: string, label: string, required: boolean) => (
    <label>{label}
      <select required={required} value={cols[key]}
              onChange={(e) => setCols({ ...cols, [key]: e.target.value })}>
        <option value="">{required ? "— pick —" : "— none —"}</option>
        {/* index-qualified key — duplicate column names (common in
            bank exports) otherwise collide React keys and drop options */}
        {need.header.map((h, i) => <option key={`${i}-${h}`} value={h}>{h}</option>)}
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
                  const j = need.header.indexOf(col);
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
              disabled={!ready || finish.isPending}
              onClick={() => finish.mutate()}>
        Import with this mapping
      </button>
    </div>
  );
}

function BatchList({ rows, onChanged }: {
  rows: { id: string; created_at: string; filename: string | null;
          source: string; row_count: number | null;
          annotations?: number }[];
  onChanged: () => void;
}) {
  const qc = useQueryClient();
  const [warning, setWarning] = useState<string | null>(null);
  const undo = useMutation({
    mutationFn: (batch_id: string) => api.importRollback(batch_id),
    onSuccess: (r, batch_id) => {
      // the undone batch leaves the table now; the refetch that follows
      // only re-proves what the server has already committed
      removeFromList<{ batches: Batch[] }, Batch>(qc, ["batches"], "batches",
                                                  (b) => b.id === batch_id);
      setWarning(r.warning); onChanged();
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
