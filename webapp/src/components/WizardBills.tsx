import { useMutation, useQuery, useQueryClient, type QueryClient }
  from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api, errText, money as money$, proposalSuperseded, toast,
         type BillsData, type Proposal, type ProposalAdjust, type SetupMode }
  from "../api/client";
import { cadenceChoices, cadenceWords } from "../cadence";
import { parseMoneyDraft } from "../moneymath";

// The setup wizard's bills step: the question (find them, or type them),
// then one of two panes. The Bills page stays the full editor; these panes
// are the short version a first-run household can finish in one sitting.

const money = (v: number | null | undefined) => money$(v, true);

const ordinal = (n: number) => {
  const t = n % 100;
  const s = t >= 11 && t <= 13 ? "th"
    : n % 10 === 1 ? "st" : n % 10 === 2 ? "nd" : n % 10 === 3 ? "rd" : "th";
  return `${n}${s}`;
};
const dayOf = (iso: string | null | undefined) =>
  iso ? parseInt(iso.slice(8, 10), 10) || null : null;
const DAYS = Array.from({ length: 31 }, (_, i) => i + 1);

/** The next calendar date falling on `day` of the month, today included;
 *  a short month takes its last day. Local time, as YYYY-MM-DD. */
export function nextDueISO(day: number, today = new Date()): string {
  const y = today.getFullYear(), m = today.getMonth();
  const on = (yy: number, mm: number) =>
    new Date(yy, mm, Math.min(day, new Date(yy, mm + 1, 0).getDate()));
  let d = on(y, m);
  if (d < new Date(y, m, today.getDate())) d = on(y, m + 1);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

/** A proposal's cadence in the editor's FREQ:interval form, or null when
 *  it has none to edit (an envelope pools by month, it has no cycle). */
const cadenceOf = (p: Proposal) =>
  p.frequency && p.bill_type !== "envelope"
    ? `${p.frequency.toUpperCase()}:${p.interval ?? 1}` : null;

/** Every write here can change the schedule, the Today verdict, the
 *  budgets prefill (it reads pending and approved income) and the wizard's
 *  own counts. */
function refetchBills(qc: QueryClient) {
  for (const k of ["bills", "today", "calendar", "budgets-suggest",
                   "onboarding"])
    qc.invalidateQueries({ queryKey: [k] });
}

export const SETUP_CHOICES: { mode: SetupMode; title: string;
                              caption: string; pick?: string }[] = [
  { mode: "auto", title: "Find them for me, I'll adjust",
    pick: "RECOMMENDED",
    caption: "The finder proposes every bill and paycheck it can prove from "
      + "your ledger and pre-fills the budget from real months. Nothing "
      + "counts until you approve it; edit or dismiss anything." },
  { mode: "manual", title: "I'll enter them myself",
    caption: "Start with an empty bills list and blank budgets. Detection "
      + "stays available later from Bills → \"Find bills & income\" and "
      + "still never adds a bill without your approval." },
];

/** Two option cards; a tap is the choice (the wizard saves it). */
export function SetupQuestion({ mode, onPick, busy }: {
  mode: SetupMode; onPick: (m: SetupMode) => void; busy?: boolean;
}) {
  return (
    <div className="wiz-choice two" role="radiogroup"
         aria-label="How do you want to set up bills and budgets">
      {SETUP_CHOICES.map((c) => (
        <button key={c.mode} type="button" role="radio"
                aria-checked={mode === c.mode} disabled={busy}
                className={"wiz-opt" + (mode === c.mode ? " sel" : "")}
                onClick={() => onPick(c.mode)}>
          {c.pick && <span className="wiz-opt-pick">{c.pick}</span>}
          <b>{c.title}</b>
          <small>{c.caption}</small>
        </button>
      ))}
    </div>
  );
}

/** Whether a cadence has a day of the month to pick. Monthly and yearly
 *  bills are anchored on a day; the server refuses a due day for any other
 *  (an envelope pools, a weekly or daily one has no day of the month, a
 *  one-time bill keeps the date it was found on). */
const takesDueDay = (cadence: string | null) => {
  const freq = (cadence ?? "").split(":")[0].toUpperCase();
  return freq === "MONTHLY" || freq === "YEARLY";
};

/** One proposal's inline Adjust form: name, amount, cadence, due day. Only
 *  what was changed is sent, so an untouched field keeps the finder's
 *  value (and its evidence) exactly — an unchanged due day included: the
 *  server anchors a monthly or yearly bill on the offer's own due date. */
function AdjustForm({ p, cadences, busy, refused, onCancel, onSave }: {
  p: Proposal; cadences: [string, string][]; busy: boolean;
  // the server's refusal of the last save (a name already taken, charges
  // another bill already tracks), shown where the person is typing
  refused: string | null;
  onCancel: () => void; onSave: (a: ProposalAdjust) => void;
}) {
  const cad0 = cadenceOf(p);
  const day0 = dayOf(p.next_due);
  const [name, setName] = useState(p.payee);
  const [amt, setAmt] = useState(Math.abs(p.amount).toFixed(2));
  const [cad, setCad] = useState(cad0 ?? "");
  const [day, setDay] = useState<number | null>(day0);
  const [err, setErr] = useState<string | null>(null);
  // the day picker follows the cadence as chosen now, not as offered
  const withDay = takesDueDay(cad || cad0);
  const save = () => {
    const a: ProposalAdjust = {};
    const n = name.trim();
    if (!n) { setErr("The bill needs a name."); return; }
    if (n !== p.payee) a.name = n;
    const v = parseMoneyDraft(amt);
    if (v === null || v <= 0) { setErr("The amount must be above $0."); return; }
    if (Math.abs(v - Math.abs(p.amount)) >= 0.005) a.amount = v;
    if (cad && cad !== cad0) a.cadence = cad;
    if (withDay && day !== null && day !== day0) a.due_day = day;
    setErr(null);
    onSave(a);
  };
  return (
    <div className="wiz-adjust">
      <div className="wiz-adjust-fields">
        <input aria-label="Bill name" value={name}
               onChange={(e) => setName(e.target.value)} />
        <input aria-label="Amount" inputMode="decimal" value={amt}
               onChange={(e) => setAmt(e.target.value)} />
        {cad0 ? (
          <select aria-label="Cadence" value={cad}
                  onChange={(e) => setCad(e.target.value)}>
            {cadenceChoices(cadences, [cad0]).map(([v, l]) => (
              <option key={v} value={v}>{l}</option>))}
          </select>
        ) : <span className="mut" style={{ alignSelf: "center" }}>
              pooled monthly</span>}
        {withDay && (
          <select aria-label="Due day" value={day ?? ""}
                  onChange={(e) => setDay(e.target.value
                    ? +e.target.value : null)}>
            {day0 === null && <option value="">due day…</option>}
            {DAYS.map((d) => (
              <option key={d} value={d}>due {ordinal(d)}</option>))}
          </select>)}
      </div>
      {(err ?? refused) && <p className="neg" style={{ margin: ".3rem 0 0",
                                          fontSize: 13 }}>{err ?? refused}</p>}
      <div className="wiz-adjust-act">
        <button type="button" onClick={onCancel} disabled={busy}>Cancel</button>
        <button type="button" className="pri" disabled={busy} onClick={save}>
          {busy ? "saving…" : "Save & approve"}</button>
      </div>
    </div>
  );
}

/** A proposal's one line: what, how often, how much, when, and the
 *  evidence count the finder gives. */
function proposalLine(p: Proposal): string {
  const cad = cadenceOf(p);
  const parts = [
    p.bill_type === "envelope" ? "~/mo pooled"
      : cad ? cadenceWords(cad) : p.cadence,
    (p.income ? "+" : "") + money(Math.abs(p.amount)),
  ];
  const d = dayOf(p.next_due);
  if (d && p.bill_type !== "envelope") parts.push(ordinal(d));
  return parts.filter(Boolean).join(" · ");
}

/** The automatic path: the finder runs once the list has loaded, and each
 *  proposal is approved, adjusted then approved, or dismissed in place. */
export function AutoBillsPane() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["bills"], queryFn: () => api.bills() });
  const [adjusting, setAdjusting] = useState<string | null>(null);
  // A 409 on an adjusted approval is about what the person typed — the
  // name is taken, or the new name's charges belong to another bill — so
  // it is answered inside the still-open form, with their values kept,
  // rather than in a toast that vanishes while they look for the field.
  const [refused, setRefused] = useState<{ pid: string; text: string } | null>(null);
  const [adding, setAdding] = useState(false);
  const dropProposal = (pid: string) =>
    qc.setQueryData(["bills"], (old?: BillsData) => old && {
      ...old, proposals: old.proposals.filter((x) => x.id !== pid) });
  const act = useMutation({
    mutationFn: ({ pid, action, adjust }: { pid: string;
      action: "approve" | "reject"; adjust?: ProposalAdjust }) =>
      api.billsProposal(pid, action, adjust),
    onSuccess: (r, v) => {
      toast(proposalSuperseded(r)
        ? `${r.payee} is already tracked — the offer was withdrawn`
        : `${v.action === "approve" ? "Approved" : "Dismissed"}: ${r.payee}`
          + (r.note ? ` — ${r.note}` : ""));
      // the server has committed: the row leaves now, not after the refetch
      dropProposal(v.pid);
      setAdjusting((cur) => (cur === v.pid ? null : cur));
      refetchBills(qc);
      if (v.action === "approve") qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onMutate: () => setRefused(null),
    onError: (e, v) => {
      // an offer already tracked is withdrawn whatever the form says, so
      // that one still leaves the list with a toast
      if (proposalSuperseded(e)) {
        toast(errText(e)); dropProposal(v.pid); refetchBills(qc); return;
      }
      if (v.adjust && e instanceof Error && e.message.startsWith("409")) {
        setRefused({ pid: v.pid, text: errText(e) }); return;
      }
      toast(errText(e));
    },
  });
  const approveAll = useMutation({
    mutationFn: api.billsApproveAll,
    onSuccess: (r) => {
      toast(r.failed.length
        ? `Approved ${r.approved}; ${r.failed.length} could not be applied `
          + `and stayed pending (${r.failed.map((f) => f.error).join("; ")})`
        : `Approved all ${r.approved} proposal${r.approved === 1 ? "" : "s"}`);
      setAdjusting(null);
      refetchBills(qc);
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e) => toast(errText(e)),
  });
  const detect = useMutation({
    mutationFn: api.billsDetect,
    onSuccess: () => refetchBills(qc),
    onError: (e) => toast(errText(e)),
  });
  // The scan waits for the list's FIRST load: fired on mount it would race
  // that load, and the invalidate after it would fold into the in-flight
  // first fetch — the pane would show the pre-scan list until a reload.
  const ran = useRef(false);
  useEffect(() => {
    if (q.isSuccess && !ran.current) { ran.current = true; detect.mutate(); }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.isSuccess]);

  if (q.isPending) return <span className="mut">loading…</span>;
  if (q.isLoadingError) return <p className="neg">Couldn't load bills: {errText(q.error)}</p>;
  const { proposals, bills, cadences } = q.data;
  const tracked = bills.filter((b) => !b.disabled);
  const busy = (pid: string) => act.isPending && act.variables?.pid === pid;
  return (
    <div>
      {detect.isPending && (
        <p className="mut" style={{ marginTop: 0 }}>
          ⟳ scanning for bills &amp; income…</p>)}
      {proposals.length > 1 && (
        <div style={{ display: "flex", justifyContent: "flex-end",
                      marginBottom: ".4rem" }}>
          <button className="pri" disabled={approveAll.isPending || act.isPending}
                  onClick={() => approveAll.mutate()}>
            {approveAll.isPending ? "approving…"
              : `Approve all ${proposals.length}`}</button>
        </div>
      )}
      <div className="wiz-rows">
        {proposals.map((p) => (
          <div key={p.id}
               className={"wiz-row" + (adjusting === p.id ? " adjusting" : "")}>
            <div className="wiz-row-main">
              <div style={{ minWidth: 0 }}>
                <b>{p.payee}</b>
                {p.income && <>{" "}<span className="pill g">income</span></>}
                {p.kind !== "add" && <>{" "}<span className="pill a">
                  {p.kind === "attach" ? "fee"
                    : p.kind === "merchant" ? "add merchant" : p.kind}</span></>}
                <br />
                <small className="mut">{proposalLine(p)}
                  {p.summary ? ` · ${p.summary}` : ""}
                  {adjusting === p.id && <> — <span style={{
                    color: "var(--blue)" }}>adjusting</span></>}</small>
              </div>
              {adjusting !== p.id && (
                <div className="wiz-row-act">
                  <button disabled={busy(p.id)}
                          onClick={() => act.mutate({ pid: p.id,
                                                      action: "reject" })}>
                    Dismiss</button>
                  {/* only a new bill or paycheck has fields to adjust (the
                      server refuses adjust on anything else); a fee,
                      merchant join, change or removal touches an existing
                      bill and is approved or dismissed as offered */}
                  {p.kind === "add" && (
                    <button disabled={busy(p.id)}
                            onClick={() => setAdjusting(p.id)}>Adjust</button>)}
                  <button className="pri" disabled={busy(p.id)}
                          onClick={() => act.mutate({ pid: p.id,
                                                      action: "approve" })}>
                    Approve</button>
                </div>
              )}
            </div>
            {adjusting === p.id && (
              <AdjustForm p={p} cadences={cadences} busy={busy(p.id)}
                refused={refused?.pid === p.id ? refused.text : null}
                onCancel={() => { setAdjusting(null); setRefused(null); }}
                onSave={(a) => act.mutate({ pid: p.id, action: "approve",
                  adjust: Object.keys(a).length ? a : undefined })} />
            )}
          </div>
        ))}
        {tracked.map((b) => (
          <div key={`bill-${b.payee}`} className="wiz-row">
            <div className="wiz-row-main">
              <div style={{ minWidth: 0 }}>
                <b>{b.payee}</b>
                {b.income && <>{" "}<span className="pill g">income</span></>}
                <br />
                <small className="mut">{b.cadence} · {money(Math.abs(b.amount))}
                  {b.due_on ? ` · next ${b.due_on.slice(5).replace("-", "/")}` : ""}
                </small>
              </div>
              <span className="pill g">approved</span>
            </div>
          </div>
        ))}
      </div>
      {!detect.isPending && proposals.length === 0 && tracked.length === 0 && (
        <p className="mut">The finder found nothing it could prove yet. Add
          the ones you know by hand below, or come back from Bills once more
          history has arrived.</p>)}
      <p className="mut" style={{ fontSize: 13 }}>
        {proposals.length} proposal{proposals.length === 1 ? "" : "s"} ·{" "}
        {tracked.length} approved. Adjust changes the name, amount, cadence
        or due day before it is approved; the finder's evidence stays
        attached.{" "}
        <button type="button" className="linkish"
                onClick={() => setAdding((a) => !a)}>
          {adding ? "Close the form" : "Add a bill by hand"}</button></p>
      {adding && <BillAddForm />}
    </div>
  );
}

const MANUAL_CADENCES: [string, string][] = [
  ["MONTHLY:1", "monthly"], ["WEEKLY:1", "weekly"],
  ["WEEKLY:2", "every 2 weeks"], ["MONTHLY:3", "every 3 months"],
  ["YEARLY:1", "yearly"],
];

/** The hand-entry form both panes share: name, cadence, amount, due day,
 *  and two buttons that say which kind it is. The same door the Bills
 *  page's add form uses. */
function BillAddForm() {
  const qc = useQueryClient();
  const [name, setName] = useState("");
  const [cad, setCad] = useState("MONTHLY:1");
  const [amt, setAmt] = useState("");
  const [day, setDay] = useState(1);
  const [err, setErr] = useState<string | null>(null);
  const add = useMutation({
    mutationFn: async (income: boolean) => {
      const v = parseMoneyDraft(amt);
      if (!name.trim()) throw new Error("Give it a name first.");
      if (v === null || v <= 0) throw new Error("The amount must be above $0.");
      return api.billsSave({ payee: name.trim(), amount: v, cadence: cad,
                             next_due: nextDueISO(day), income });
    },
    onSuccess: (r, income) => {
      toast(`${income ? "Paycheck" : "Bill"} added: ${r.payee}`);
      setName(""); setAmt(""); setErr(null);
      refetchBills(qc);
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
    onError: (e) => setErr(errText(e)),
  });
  return (
    <div className="wiz-addform">
      <div className="wiz-field">
        <input placeholder="Bill name (e.g. Rent)" value={name}
               aria-label="Bill name"
               onChange={(e) => setName(e.target.value)} />
        <select aria-label="Cadence" value={cad}
                onChange={(e) => setCad(e.target.value)}>
          {MANUAL_CADENCES.map(([v, l]) => (
            <option key={v} value={v}>{l}</option>))}
        </select>
      </div>
      <div className="wiz-field">
        <input placeholder="Amount" inputMode="decimal" value={amt}
               aria-label="Amount"
               onChange={(e) => setAmt(e.target.value)} />
        <select aria-label="Due day" value={day}
                onChange={(e) => setDay(+e.target.value)}>
          {DAYS.map((d) => (
            <option key={d} value={d}>due on the {ordinal(d)}</option>))}
        </select>
      </div>
      <div style={{ margin: ".4rem 0 .8rem", display: "flex", gap: ".5rem",
                    flexWrap: "wrap" }}>
        <button type="button" disabled={add.isPending}
                onClick={() => add.mutate(false)}>+ Add bill</button>
        <button type="button" disabled={add.isPending}
                onClick={() => add.mutate(true)}>+ Add paycheck</button>
      </div>
      {err && <p className="neg" style={{ marginTop: 0, fontSize: 13 }}>{err}</p>}
    </div>
  );
}

/** The manual path: the add form and what has been added so far. The
 *  finder does not run here — the household said it would type them. */
export function ManualBillsPane() {
  const q = useQuery({ queryKey: ["bills"], queryFn: () => api.bills() });
  const bills = (q.data?.bills ?? []).filter((b) => !b.disabled);
  const nBills = bills.filter((b) => !b.income).length;
  const nInc = bills.length - nBills;
  return (
    <div>
      <BillAddForm />
      <div className="wiz-rows">
        {bills.map((b) => (
          <div key={b.payee} className="wiz-row">
            <div className="wiz-row-main">
              <div style={{ minWidth: 0 }}>
                <b>{b.payee}</b>
                {b.income && <>{" "}<span className="pill g">income</span></>}
                <br />
                <small className="mut">{b.cadence} · {money(Math.abs(b.amount))}
                  {dayOf(b.due_on) ? ` · ${ordinal(dayOf(b.due_on)!)}` : ""}
                </small>
              </div>
              <span className="pill g">added</span>
            </div>
          </div>
        ))}
      </div>
      <p className="mut" style={{ fontSize: 13 }}>
        {nBills} bill{nBills === 1 ? "" : "s"} · {nInc} income. Nothing here
        was detected; the finder is off for this household until you turn it
        on.</p>
    </div>
  );
}
