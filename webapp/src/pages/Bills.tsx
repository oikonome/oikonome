// Bills — the schedule's monthly load as the headline, then proposals, then
// ONE table for every cadence (thin group separators, envelopes apart because
// their columns differ), inline edit rows, and the 5-week calendar.
// Status is ONE column, not a state pill beside a health pill: a healthy
// bill says nothing, so the only coloured rows want a decision.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, errText, mmdd, money as money$, toast, type Bill, type BillsData, catLabel } from "../api/client";
import { patchList, removeFromList } from "../api/cache";
import { canEdit } from "../role";
const money = (v: number | null | undefined) => money$(v, true);  // cents on transaction/recurring surfaces


// cadence select value → group heading (the list order is the rank)
const GROUPS: [string, string][] = [
  ["WEEKLY:1", "Weekly"], ["DAILY:14", "Every 14 days"],
  ["WEEKLY:2", "Every 2 weeks"], ["MONTHLY:1", "Monthly"],
  ["ENVELOPE:1", "Monthly envelope"], ["ENVELOPE:12", "Annual envelope"],
  ["WEEKLY:8", "Every 8 weeks"],
  ["MONTHLY:2", "Every 2 months"], ["MONTHLY:3", "Every 3 months"],
  ["MONTHLY:6", "Every 6 months"], ["YEARLY:1", "Yearly"],
  ["YEARLY:2", "Every 2 years"],
  ["ONE_TIME:1", "One-time / other"],
];
const groupOf = (cadence: string) =>
  GROUPS.find(([v]) => v === cadence)?.[1] ?? "One-time / other";

// The ADD pickers stay short: the common cadences + the
// envelopes; anything unusual (every 2 weeks/3 months/2 years…) is one
// "Custom interval…" click. GROUPS above still labels + groups every cadence,
// and inline EDIT keeps the full server list, so existing bills are unaffected.
export const SIMPLE_CADENCES: [string, string][] = [
  ["DAILY:1", "daily"], ["WEEKLY:1", "weekly"], ["MONTHLY:1", "monthly"],
  ["YEARLY:1", "yearly"], ["ENVELOPE:1", "envelope (monthly pool)"],
  ["ENVELOPE:12", "envelope (annual pool)"],
];
const groupRank = (name: string) => {
  const i = GROUPS.findIndex(([, g]) => g === name);
  return i === -1 ? 90 : i;
};

interface Row extends Bill {
  cadenceLabel: string;
}

// cycle bar: how far through the billing cycle the row is — fills as the
// due date approaches, in the envelopes' own visual language
const DAY = 86_400_000;
const daysUntil = (isoDate: string) => {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return Math.round((new Date(isoDate + "T00:00:00").getTime() - today.getTime()) / DAY);
};
const cycleDays = (cad: string) => {
  const [freq, n] = cad.split(":");
  const iv = parseInt(n, 10) || 1;
  const per = freq === "DAILY" ? 1 : freq === "WEEKLY" ? 7
    : freq === "MONTHLY" ? 30.44 : freq === "YEARLY" ? 365.25 : 0;
  return per * iv;
};
// The bills list serves a human cadence label ("monthly", "every 2 weeks")
// beside the FREQ:interval select value; when a row arrives without the
// value, parse the label back into one so the cycle bar can still size
// itself. (GROUPS names are display headings; matching against them misses
// daily and any interval the heading list doesn't enumerate.)
const LABEL_UNITS: Record<string, string> = {
  da: "DAILY", week: "WEEKLY", month: "MONTHLY", year: "YEARLY" };
const SIMPLE_LABEL_CADENCE: Record<string, string> = {
  daily: "DAILY:1", weekly: "WEEKLY:1", monthly: "MONTHLY:1", yearly: "YEARLY:1" };
const cadenceFromLabel = (label: string): string => {
  const m = /^every (\d+) (da|week|month|year)/.exec(label);
  return m ? `${LABEL_UNITS[m[2]]}:${m[1]}` : SIMPLE_LABEL_CADENCE[label] ?? "";
};
const dueText = (n: number, income?: boolean) =>
  n === 0 ? (income ? "today" : "due today")
  : n === 1 ? (income ? "tomorrow" : "due tomorrow")
  : n === -1 ? "1 day late"
  : n < 0 ? `${-n} days late`
  : `in ${n} days`;
// the cycle bar turns amber this close to the due date (the words beside
// it say the number; the colour says "soon" from across the room)
const SOON_DAYS = 3;

const hpill = (h: string | null,
               seen?: { date: string; amount: number } | null) =>
  h === "ok" ? <span className="pill g hide-m" title="ledger matches">✓</span>
  : h === "drifting" ? <span className="pill a" title="amount wandering — auto-corrects tonight">drifting</span>
  : h === "stale" ? <span className="pill r" title="a full cycle with no matching charge">stale</span>
  : h === "mismatch" ? <span className="pill a" title={"merchant is in the ledger but never near the configured amount"
        + (seen ? ` — last seen ${money(seen.amount)} on ${mmdd(seen.date)}` : "")}>mismatch</span>
  : h === "misdated" ? <span className="pill a" title="payments match, but the due-date anchor is off the real draft day">misdated</span>
  : <span className="pill m" title="never seen in the ledger (cash/manual bill?)">off-ledger</span>;

// .linklike carries no CSS of its own — the base button rule still paints a
// border and card background until these are overridden inline, as every
// other .linklike in the app does.
const LINKLIKE = { padding: 0, background: "none", border: "none",
                   cursor: "pointer" } as const;

// the ✕/↺ that dismiss or restore a row's flag: same chrome, muted
const FLAGBTN = { padding: ".08rem .3rem", fontSize: 11, border: "none",
                  background: "none", color: "var(--mut)",
                  cursor: "pointer" } as const;

// autoDetect (wizard): run the finder on mount so the step opens with
// proposals already on screen — confirm, don't hunt for a button
export default function Bills({ autoDetect = false }:
                                   { autoDetect?: boolean } = {}) {
  const qc = useQueryClient();
  const [sp, setSp] = useSearchParams();
  const edit = sp.get("edit") ?? "";
  const q = useQuery({ queryKey: ["bills"], queryFn: api.bills });
  // detect / approve / edit / archive are editing writes — viewers keep
  // the full read view (groups, calendar, proposals as information)
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  const [adding, setAdding] = useState(false);
  // the triage queue's undo affordance: one reversible action at a time
  const [undo, setUndo] = useState<{ label: string;
                                     act: () => void } | null>(null);
  const jumpToRow = (payee: string) => {
    const el = document.getElementById(`bill-${payee}`);
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
    el?.animate([{ backgroundColor: "rgba(96,165,250,.28)" },
                 { backgroundColor: "transparent" }], { duration: 1800 });
  };

  const bills = q.data?.bills ?? [];
  // A link that opens a row's editor — the Today alert strip, a bill's
  // history page, the attention list — arrives as ?edit=payee. The page
  // scrolls to the top on arrival (a new path), so the opened editor has
  // to be brought into view itself, once the rows exist to scroll to.
  const scrolledTo = useRef<string | null>(null);
  useEffect(() => {
    if (!edit || !bills.length || scrolledTo.current === edit) return;
    const el = document.getElementById(`bill-${edit}`);
    if (!el) return;
    scrolledTo.current = edit;
    el.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [edit, bills.length]);
  // the rows behind the "need attention" count (same rule the server uses).
  // A disabled bill shows no health pill and no dismiss control, so jumping
  // to one would land on a row with nothing to act on.
  const attnRows = bills.filter((b) =>
    ["stale", "mismatch", "misdated", "drifting"].includes(b.health ?? "")
    && !b.health_dismissed && !b.disabled);

  // Every row carries its own edit-form fields (the list is ONE request;
  // there is no per-bill query to refetch), so a write that changes a bill
  // refetches the list and the pages that read the schedule.
  const refetchAll = () => {
    qc.invalidateQueries({ queryKey: ["bills"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["calendar"] });
    // step-3 budgets prefill from pending/approved income + bills
    qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    qc.invalidateQueries({ queryKey: ["settings"] });
  };

  // Rejecting decides a proposal and writes nothing else: no bill changed,
  // so the calendar and the settings cannot have moved. The list itself,
  // the budgets prefill — which reads PENDING income proposals when nothing
  // is approved yet — and the two places that count pending proposals:
  // Today's "N proposed changes awaiting review" and the onboarding line.
  const refetchAfterReject = () => {
    qc.invalidateQueries({ queryKey: ["bills"] });
    qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["onboarding"] });
  };

  const act = useMutation({
    mutationFn: ({ pid, action }: { pid: string; action: "approve" | "reject" }) =>
      api.billsProposal(pid, action),
    onSuccess: (r, v) => {
      toast(`${v.action === "approve" ? "Approved" : "Rejected"}: ${r.payee}`);
      // drop the decided proposal from the cached list NOW: the server has
      // already committed it, and waiting for the refetch to prove it left
      // the row sitting there through every other query this triggers.
      qc.setQueryData(["bills"], (old?: BillsData) => old && {
        ...old,
        proposals: old.proposals.filter((p) => p.id !== v.pid),
      });
      // approving writes one bill; rejecting writes none
      if (v.action === "approve") refetchAll();
      else refetchAfterReject();
    },
    onError: (e) => toast(errText(e)),
  });

  const approveAll = useMutation({
    mutationFn: api.billsApproveAll,
    onSuccess: (r) => {
      toast(r.failed.length
        ? `Approved ${r.approved}; ${r.failed.length} could not be applied `
          + `and stayed pending (${r.failed.map((f) => f.error).join("; ")})`
        : `Approved all ${r.approved} proposal${r.approved === 1 ? "" : "s"}`);
      refetchAll();
    },
    onError: (e) => toast(errText(e)),
  });

  const detect = useMutation({
    mutationFn: api.billsDetect,
    onSuccess: (r) => {
      const nInc = r.proposed_income ?? 0;
      const nBill = Math.max(0, (r.proposed_add ?? 0) - nInc);
      const parts: string[] = [];
      if (nInc) parts.push(
        `${nInc} income series`);
      if (nBill) parts.push(
        `${nBill} bill${nBill === 1 ? "" : "s"}`);
      if (!parts.length && r.proposed_add)
        parts.push(`${r.proposed_add} proposal${r.proposed_add === 1 ? "" : "s"}`);
      const found = parts.length
        ? parts.join(" + ")
        : "no new proposals";
      toast(
        `Scan complete: ${found}` +
        (r.drift_applied
          ? `, ${r.drift_applied} amount${r.drift_applied === 1 ? "" : "s"} updated`
          : "") + ".");
      refetchAll();
    },
    onError: (e) => toast(errText(e)),
  });

  const toggle = useMutation({
    mutationFn: (v: string | { payee: string; disabled: boolean }) =>
      typeof v === "string" ? api.billsToggle(v)
                            : api.billsToggle(v.payee, v.disabled),
    onSuccess: (r, v) => {
      toast(`${r.payee}: ${r.disabled ? "disabled (excluded from budget)" : "enabled"}`);
      // the row and its editor read `disabled` from the list — flip it from
      // the response, then refetch
      patchList<BillsData, Bill>(qc, ["bills"], "bills",
        (b) => b.payee === r.payee, { disabled: r.disabled });
      // the triage queue's Disable arms its Undo HERE, not at click time
      // (as acceptAmount does) — armed before the write is confirmed, a
      // failed disable showed "Disabled X. [Undo]" for a bill that was never
      // disabled, with a live Undo that would fire blind later
      if (typeof v !== "string" && v.disabled) {
        setUndo({ label: `Disabled ${r.payee}.`,
                  act: () => toggle.mutate(
                    { payee: r.payee, disabled: false }) });
      }
      refetchAll();
    },
    onError: (e) => toast(errText(e)),
  });

  // the triage queue's one-tap "Accept $X": the bill takes the amount the
  // ledger has settled on (the server's fit — median of recent charges).
  // Just an edit of the bill, so Undo is saving the old amount back.
  const acceptAmount = useMutation({
    mutationFn: ({ b, amt }: { b: Bill; amt: number }) =>
      api.billsSave({ payee: b.payee, amount: amt,
                      cadence: b.cadence_value, income: b.income,
                      ...(b.due_on ? { next_due: b.due_on } : {}) }),
    onSuccess: (_r, v) => {
      setUndo({ label: `${v.b.payee} updated to ${money(v.amt)}.`,
                act: () => acceptAmount.mutate(
                  { b: { ...v.b, amount: v.b.amount }, amt: v.b.amount }) });
      refetchAll();
    },
    onError: (e) => toast(errText(e)),
  });

  const del = useMutation({
    mutationFn: ({ payee, mode }: { payee: string; mode: "archive" | "purge" }) =>
      api.billsDelete(payee, mode),
    onSuccess: (r, v) => {
      toast(r.mode === "purge" ? "Deleted forever." : "Archived (restorable).");
      setSp({}, { replace: true });
      // the row leaves its table NOW; an archive also lands in the
      // Archived list from what the page already knows about the bill.
      // The server has committed either way.
      qc.setQueryData(["bills"], (old?: BillsData) => {
        if (!old) return old;
        const gone = old.bills.find((b) => b.payee === v.payee);
        return {
          ...old,
          bills: old.bills.filter((b) => b.payee !== v.payee),
          archived: [
            ...old.archived.filter((a) => a.payee !== v.payee),
            ...(r.mode === "purge" || !gone ? [] : [{
              payee: gone.payee, amount: gone.amount,
              frequency: gone.cadence_value
                ? gone.cadence_value.split(":")[0] : null,
              synced_at: new Date().toISOString(),
            }]),
          ],
        };
      });
      refetchAll();
    },
    onError: (e) => toast(errText(e)),
  });

  const restore = useMutation({
    mutationFn: (payee: string) => api.billsRestore(payee),
    onSuccess: (_r, payee) => {
      toast("Restored.");
      // out of Archived at once; the live row arrives with the list refetch
      // (its full shape — health, occurrences — is the server's to compute)
      removeFromList<BillsData, BillsData["archived"][number]>(
        qc, ["bills"], "archived", (a) => a.payee === payee);
      refetchAll();
    },
    onError: (e) => toast(errText(e)),
  });

  // dismissing or restoring a flag writes one boolean on one bill: the pill
  // goes (or returns) from the cached list in the same frame, and only the
  // list — which also carries the "need attention" count — is refetched
  const hint = useMutation({
    mutationFn: ({ payee, action, target }: {
      payee: string; action: "dismiss" | "restore"; target: "fit" | "health" }) =>
      api.billsHint(payee, action, target),
    onSuccess: (r, v) => {
      // the server reports dismissed:true even when the bill's health is
      // one it keeps no dismissal for (it stores nothing, and the refetch
      // would bring the pill straight back) — so a health dismissal only
      // sticks on the row when its health is a dismissable problem
      patchList<BillsData, Bill>(qc, ["bills"], "bills",
        (b) => b.payee === v.payee,
        v.target === "health"
          ? (b) => ({ ...b, health_dismissed: r.dismissed
              && ["stale", "mismatch", "misdated", "drifting"]
                .includes(b.health ?? "") })
          : { hint_dismissed: r.dismissed });
      qc.invalidateQueries({ queryKey: ["bills"] });
    },
    onError: (e, v) => window.dispatchEvent(new CustomEvent("oiko-toast",
      { detail: `${v.action === "dismiss" ? "Dismiss" : "Restore"} failed: ${errText(e)}` })),
  });

  // The wizard's auto-scan waits for the page's FIRST load to land before
  // it runs. Fired on mount it races that load: the scan writes its
  // proposals while GET /api/bills is already in flight, and the invalidate
  // that follows is folded into that in-flight request (react-query does
  // not cancel a query's very first fetch — there is no data to protect),
  // so the page renders the pre-scan snapshot — "Scan complete: 6 bills"
  // over an empty list — until a reload. With data present, invalidate
  // cancels and refetches, and the proposals appear.
  const autoRan = useRef(false);
  useEffect(() => {
    if (autoDetect && q.isSuccess && !autoRan.current) {
      autoRan.current = true;
      detect.mutate();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoDetect, q.isSuccess]);

  if (q.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;
  if (q.isError)
    return <div className="card">Couldn't load bills: {String(q.error)}</div>;

  const { proposals, archived, cadences } = q.data;
  const rows: Row[] = bills.map((b) => ({ ...b, cadenceLabel: b.cadence }));
  // expense bills are amount<0 (bills_status); income bills — paychecks,
  // modelled as recurring rows — get their own section at the very top,
  // because they are the anchor number the bill groups below are paid from
  const incomeRows = rows.filter((r) => r.income);
  // group by cadence (legacy rank order); within a group, order by next due
  const groups = new Map<string, Row[]>();
  for (const r of rows) {
    if (r.income) continue;
    const g = r.cadence_value ? groupOf(r.cadence_value)
      : r.cadenceLabel === "annual envelope" ? "Annual envelope"
      : r.cadenceLabel === "envelope" ? "Monthly envelope"
      : GROUPS.find(([, name]) => name.toLowerCase() === r.cadenceLabel)?.[1]
        ?? r.cadenceLabel.charAt(0).toUpperCase() + r.cadenceLabel.slice(1);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g)!.push(r);
  }
  const ordered = [...groups.entries()].sort(
    (a, b) => groupRank(a[0]) - groupRank(b[0]) || a[0].localeCompare(b[0]));
  for (const [, g] of ordered)
    g.sort((a, b) => (a.due_on ?? "9999-99-99").localeCompare(b.due_on ?? "9999-99-99"));

  const totals = q.data.totals;
  const rowHandlers = {
    onEdit: (p: string | null) =>
      setSp(p ? { edit: p } : {}, { replace: true }),
    onToggle: (p: string) => toggle.mutate(p),
    onArchive: (p: string) => del.mutate({ payee: p, mode: "archive" }),
    onPurge: (p: string) => del.mutate({ payee: p, mode: "purge" }),
    onSaved: () => {
      setSp({}, { replace: true });
      refetchAll();
    },
    onHint: (payee: string, action: "dismiss" | "restore",
             target: "fit" | "health") => hint.mutate({ payee, action, target }),
    hintBusy: hint.isPending ? hint.variables.payee : null,
    // a slow archive/purge/toggle must not accept a second click — a double
    // toggle silently nets to no change, a double purge double-fires
    togglePending: toggle.isPending,
    pendingDelete: del.isPending && del.variables ? del.variables.mode : null,
    prefill: { pamt: sp.get("pamt") ?? "", pcad: sp.get("pcad") ?? "",
               pdue: sp.get("pdue") ?? "" },
    cadences,
  };
  return (
    <>
      <h1>{autoDetect ? "Bills & Income" : "Bills"}</h1>
      {/* HERO: what the schedule costs, which is otherwise a sum of a
          dozen cadence groups done by eye. Both figures are the budget's own
          evened-out monthly load, so this page and the Budget flow line
          cannot disagree about the same bills. */}
      {!autoDetect && (
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", gap: "1rem", alignItems: "baseline",
                      flexWrap: "wrap" }}>
          {totals && (
            <span style={{ fontSize: "1.5rem", fontWeight: 700,
                           letterSpacing: "-.01em" }}>
              {money(totals.bills_monthly)}
              <span className="sub" style={{ fontWeight: 400 }}>/mo bills</span>
              {" · "}
              <span className="pos">{money(totals.income_monthly)}</span>
              <span className="sub" style={{ fontWeight: 400 }}>/mo income</span>
              {" · "}
              {/* the difference is what the variable budget and savings
                  actually have to work with — the number the two figures
                  beside it exist to produce */}
              <span className={totals.income_monthly - totals.bills_monthly
                                 >= 0 ? "pos" : "neg"}>
                {money(totals.income_monthly - totals.bills_monthly)}</span>
              <span className="sub" style={{ fontWeight: 400 }}>
                /mo left for spending &amp; savings</span>
            </span>
          )}
          <span style={{ marginLeft: "auto", display: "flex", gap: ".5rem",
                         flexWrap: "wrap" }}>
            {/* manual add stays LEFT of the finder — for bills the
                detector won't catch (annual/biennial/one-off) */}
            {mayEdit && (
              <button disabled={detect.isPending}
                      onClick={() => setAdding((a) => !a)}>
                {adding ? "cancel" : "+ Add a bill"}
              </button>
            )}
            {mayEdit && (
              <button className="pri" disabled={detect.isPending}
                      onClick={() => detect.mutate()}>
                {detect.isPending ? "scanning…" : "Find bills & income"}
              </button>
            )}
          </span>
        </div>
        <div className="sub" style={{ marginTop: ".2rem", display: "flex",
                                      gap: ".9rem", flexWrap: "wrap",
                                      alignItems: "center" }}>
          {totals && totals.week_count > 0 && (
            <span>next 7 days: <b style={{ color: "var(--ink)" }}>
              {money(totals.week_total)}</b> across {totals.week_count}{" "}
              bill{totals.week_count === 1 ? "" : "s"}</span>
          )}
          {proposals.length > 0 && (
            <a href="#proposals" style={{ textDecoration: "none" }}>
              <span className="pill a" style={{ marginRight: ".3rem" }}>
                {proposals.length}</span>
              proposal{proposals.length === 1 ? "" : "s"} to review</a>
          )}
        </div>
        {/* the triage queue: each flagged bill is one line — the story the
            ledger tells, plus the ONE action the server's evidence supports
            (bills._suggest picks it; the client renders it verbatim). A
            weak signal degrades the primary to Review, which jumps to the
            row instead of acting. Every action is reversible and offers
            Undo, so nothing destructive ever rides one tap. */}
        {attnRows.length > 0 && (
          <div style={{ marginTop: ".55rem", border: "1px solid var(--line)",
                        borderRadius: 9, overflow: "hidden" }}>
            {attnRows.map((b) => (
              <AttentionLine key={b.payee} b={b} mayEdit={mayEdit}
                busy={(toggle.isPending
                        && (typeof toggle.variables === "string"
                              ? toggle.variables
                              : toggle.variables?.payee) === b.payee)
                      || (acceptAmount.isPending
                          && acceptAmount.variables?.b.payee === b.payee)
                      || (hint.isPending
                          && hint.variables?.payee === b.payee)}
                onJump={() => jumpToRow(b.payee)}
                onAccept={(amt) => acceptAmount.mutate({ b, amt })}
                onDisable={() => {
                  // the toggle door is a true FLIP — a second click while
                  // the first is in flight would silently net to no change
                  if (toggle.isPending) return;
                  // Undo is armed in toggle's onSuccess, once the server
                  // has actually disabled the bill
                  toggle.mutate({ payee: b.payee, disabled: true });
                }}
                onDismiss={() => hint.mutate({ payee: b.payee,
                                               action: "dismiss",
                                               target: "health" })} />
            ))}
          </div>
        )}
        {undo && (
          <div className="note" style={{ marginTop: ".4rem" }}>
            {undo.label}{" "}
            <button className="linklike" style={LINKLIKE}
                    disabled={toggle.isPending || acceptAmount.isPending}
                    onClick={() => { undo.act(); setUndo(null); }}>
              Undo</button>{" "}
            <button className="linklike" style={LINKLIKE}
                    onClick={() => setUndo(null)}>✕</button>
          </div>
        )}
      </div>
      )}
      {autoDetect && detect.isPending && (
        <span className="mut">⟳ scanning for bills &amp; income…</span>
      )}
      {adding && (
        <AddBillForm onCancel={() => setAdding(false)}
                     onSaved={() => {
                       setAdding(false);
                       toast("Bill added.");
                       refetchAll();
                     }} />
      )}

      {/* income first: paychecks are the page's anchor number — the
          money everything below is paid from */}
      {incomeRows.length > 0 && (
        <div className="card">
          <h2>Income</h2>
          <BillTable groups={[["Income", incomeRows]]} isEnv={false} income
                     edit={edit} mayEdit={mayEdit} {...rowHandlers} />
        </div>
      )}

      {proposals.length > 0 && (
        <div className="card" id="proposals"
             style={{ borderColor: "rgba(240,180,41,.45)" }}>
          <div style={{ display: "flex", alignItems: "baseline", gap: ".8rem",
                        flexWrap: "wrap" }}>
            <h2 style={{ marginRight: "auto" }}>Proposed changes</h2>
            {/* one click for the common case — the finder's first pass over
                a fresh history is usually right; the per-row buttons stay
                for the exceptions */}
            {mayEdit && proposals.length > 1 && (
              <button className="pri" style={{ alignSelf: "center" }}
                      disabled={approveAll.isPending || act.isPending}
                      onClick={() => approveAll.mutate()}>
                {approveAll.isPending ? "approving…"
                  : `Approve all ${proposals.length}`}
              </button>
            )}
          </div>
          {/* c-* cell classes + .bills-props: at <=620px index.css restacks
              each proposal into a card-like grid (columns don't fit a phone) */}
          <table className="bills-props">
            <thead>
              <tr><th>Change</th><th>Payee</th><th className="num">Amount</th>
                  <th>Cadence</th>
                  <th className="hide-m">Evidence</th><th></th></tr>
            </thead>
            <tbody>
              {proposals.map((p) => (
                <tr key={p.id}>
                  <td className="c-kind" style={{ whiteSpace: "nowrap" }}>
                    <span className={"pill " + (p.kind === "remove" ? "r" : p.kind === "add" ? "g" : "a")}>
                      {p.kind === "attach" ? "fee"
                        : p.kind === "merchant" ? "merchant" : catLabel(p.kind)}</span>
                    {/* income proposals (paychecks) flagged distinctly */}
                    {p.income && <>{" "}<span className="pill g"
                      title="detected income / paycheck">income</span></>}
                    {p.bill_type === "envelope" && <>{" "}<span className="pill m">envelope</span></>}
                    {/* the LLM tag had its own column ("Looks like") holding
                        one short word; it belongs beside the kind it
                        qualifies */}
                    {p.llm_tag && <>{" "}<span className="pill m">{p.llm_tag}</span></>}
                  </td>
                  <td className="c-payee"
                      style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    <Link to={`/bills/history?payee=${encodeURIComponent(p.payee)}`}>
                      {p.payee}</Link>
                    {p.kind === "attach" && p.bill_payee ? (
                      <span className="mut"> — a fee of {p.bill_payee}</span>) : null}
                    {/* the ledger now files the bill's charges under this
                        merchant too (a rename, a merge): join it to the bill */}
                    {p.kind === "merchant" && p.bill_payee ? (
                      <span className="mut"> — pays {p.bill_payee}</span>) : null}
                  </td>
                  <td className="num c-amt">{money(p.amount)}</td>
                  <td className="mut c-cad">
                    {p.bill_type === "envelope" ? "~/mo pooled"
                      : p.frequency
                        ? <>{p.frequency.toLowerCase()}
                            {(p.interval ?? 1) > 1 ? ` ×${p.interval}` : ""}
                            {p.next_due ? ` · next ${mmdd(p.next_due)}` : ""}</>
                        : p.cadence}
                  </td>
                  <td className="mut hide-m c-ev" title={p.summary}
                      style={{ maxWidth: "22rem", overflow: "hidden", textOverflow: "ellipsis",
                               whiteSpace: "nowrap" }}>{p.summary}</td>
                  <td className="c-act" style={{ whiteSpace: "nowrap" }}>
                    {mayEdit && (<>
                    <button className="pri"
                            disabled={act.isPending && act.variables.pid === p.id}
                            onClick={() => act.mutate({ pid: p.id, action: "approve" })}>
                      approve
                    </button>{" "}
                    <button disabled={act.isPending && act.variables.pid === p.id}
                            onClick={() => act.mutate({ pid: p.id, action: "reject" })}>
                      reject
                    </button>
                    </>)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* ONE table for every cadence, with thin group separators. A card
          per group repeats the same five column headers a dozen times; the
          separators keep the grouping — and the shared column alignment the
          fixed colgroup exists for — at a fraction of the height. Envelopes
          stay their own card: their columns really are different. */}
      {groupsOf(ordered, false).length > 0 && (
        <div className="card" id="bills">
          <h2>Bills</h2>
          <BillTable groups={groupsOf(ordered, false)} isEnv={false}
                     edit={edit} mayEdit={mayEdit} {...rowHandlers} />
          <div className="sub" style={{ marginTop: ".5rem" }}>
            A healthy bill carries no status pill — a quiet row <b>is</b> the ✓.
          </div>
        </div>
      )}
      {groupsOf(ordered, true).length > 0 && (
        <div className="card">
          <h2>Envelopes <span className="sub" style={{ fontWeight: 400 }}>
            — pooled caps; overflow counts as variable spending</span></h2>
          <BillTable groups={groupsOf(ordered, true)} isEnv
                     edit={edit} mayEdit={mayEdit} {...rowHandlers} />
        </div>
      )}

      {/* the 5-week bill calendar, after the parity tables. Deliberately
          NOT in the setup wizard: it pushes the step's Continue bar below
          the fold right after Approve all; the Bills page itself keeps it,
          and Archived follows it directly */}
      {bills.length > 0 && !autoDetect && <UpcomingCalendar />}

      {archived && archived.length > 0 && (
        <details className="card">
          <summary style={{ cursor: "pointer", color: "var(--mut)" }}>
            Archived — {archived.length} removed bill{archived.length > 1 ? "s" : ""} (history kept)
          </summary>
          {/* .bills-arch + c-*: restacked at <=620px like the live groups */}
          <table className="bills-arch" style={{ marginTop: ".6rem" }}>
            <thead><tr><th>Bill</th><th className="num">Amount</th><th>Cadence</th>
              <th>Archived</th><th></th></tr></thead>
            <tbody>
              {archived.map((a) => (
                <tr key={a.payee}>
                  <td className="c-name"
                      style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                    <Link to={`/bills/history?payee=${encodeURIComponent(a.payee)}`}>
                      {a.payee}</Link></td>
                  <td className="num c-amt">{money(Math.abs(a.amount))}</td>
                  <td className="mut c-cad">{catLabel(a.frequency || "one-time").toLowerCase()}</td>
                  <td className="mut c-arch" style={{ whiteSpace: "nowrap" }}>
                    {a.synced_at ? mmdd(a.synced_at.slice(0, 10)) : "·"}</td>
                  <td className="c-act" style={{ whiteSpace: "nowrap" }}>
                    {mayEdit && (<>
                    <button disabled={restore.isPending && restore.variables === a.payee}
                            onClick={() => restore.mutate(a.payee)}>restore</button>{" "}
                    <button disabled={del.isPending && del.variables?.payee === a.payee}
                            onClick={() => {
                      if (confirm("Permanently delete this archived bill? The ledger " +
                                  "transactions stay; only this bill record is removed."))
                        del.mutate({ payee: a.payee, mode: "purge" });
                    }}>delete forever</button>
                    </>)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}

      {bills.length === 0 && proposals.length === 0 && (
        <div className="card">
          <p>No recurring bills yet. Import or connect your bank, then hit
            <b> Find recurring bills</b> — the finder proposes everything it
            can prove from your ledger.</p>
        </div>
      )}
    </>
  );
}

// Envelope cadences render with a different column set, so they are split
// out to their own card rather than sharing the bills table.
const isEnvGroup = (name: string) =>
  name === "Monthly envelope" || name === "Annual envelope";
const groupsOf = (ordered: [string, Row[]][], env: boolean) =>
  ordered.filter(([name]) => isEnvGroup(name) === env);

/** One table for many cadence groups: a thin separator row introduces each,
 * replacing a card-with-its-own-thead per group. */
function BillTable({ groups, isEnv, income, edit, mayEdit, onEdit, onToggle,
                     onArchive, onPurge, onSaved, onHint, hintBusy,
                     togglePending, pendingDelete, prefill, cadences }: {
  groups: [string, Row[]][]; isEnv: boolean; income?: boolean;
  edit: string; mayEdit: boolean;
  onEdit: (payee: string | null) => void;
  onToggle: (payee: string) => void;
  onArchive: (payee: string) => void;
  onPurge: (payee: string) => void;
  onSaved: () => void;
  onHint: (payee: string, action: "dismiss" | "restore",
           target: "fit" | "health") => void;
  hintBusy: string | null;
  togglePending: boolean;
  pendingDelete: "archive" | "purge" | null;
  prefill: { pamt: string; pcad: string; pdue: string };
  cadences: [string, string][];
}) {
  const cols = isEnv ? 5 : 6;
  return (
    // income indents too — its rows align with the bill rows below even
    // though its single group renders no separator
    <table className={groups.length > 1 || income ? "bills-table grouped" : "bills-table"}
           style={{ tableLayout: "fixed" }}>
      {isEnv ? (
        <colgroup><col /><col style={{ width: "13%" }} />
          <col style={{ width: "26%" }} /><col style={{ width: "17%" }} />
          <col style={{ width: "3.4rem" }} /></colgroup>
      ) : (
        <colgroup><col /><col style={{ width: "13%" }} />
          <col style={{ width: "10%" }} /><col style={{ width: "20%" }} />
          <col style={{ width: "16%" }} /><col style={{ width: "3.4rem" }} /></colgroup>
      )}
      <thead>
        {isEnv ? (
          <tr><th>Envelope</th><th className="num">Pool</th><th>Used</th>
            <th>Status</th><th></th></tr>
        ) : (
          <tr><th>{income ? "Source" : "Bill"}</th>
            <th className="num">Amount</th>
            <th>{income ? "Next expected" : "Next due"}</th>
            <th>Cycle</th>
            <th>Status</th><th></th></tr>
        )}
      </thead>
      <tbody>
        {groups.map(([name, rows]) => (
          <Fragment key={name}>
            {/* the separator carries what a whole card heading would.
                One group needs none — the card's own h2 already says it. */}
            {groups.length > 1 && (
              <tr className="grp"><td colSpan={cols}>{name}</td></tr>)}
            {rows.map((b) => (
              <BillRows key={b.payee} b={b} isEnv={isEnv}
                        cols={cols} editing={mayEdit && edit === b.payee}
                        mayEdit={mayEdit}
                        onEdit={onEdit} onToggle={onToggle}
                        onArchive={onArchive} onPurge={onPurge}
                        onSaved={onSaved} onHint={onHint}
                        hintBusy={hintBusy === b.payee}
                        togglePending={togglePending}
                        pendingDelete={pendingDelete} prefill={prefill}
                        cadences={cadences} />
            ))}
          </Fragment>
        ))}
      </tbody>
    </table>
  );
}

function BillRows({ b, isEnv, cols, editing, mayEdit, onEdit,
                    onToggle, onArchive, onPurge, onSaved, onHint, hintBusy,
                    togglePending, pendingDelete, prefill, cadences }: {
  b: Row; isEnv: boolean; cols: number; editing: boolean;
  mayEdit: boolean;
  onEdit: (payee: string | null) => void;
  onToggle: (payee: string) => void;
  onArchive: (payee: string) => void;
  onPurge: (payee: string) => void;
  onSaved: () => void;
  onHint: (payee: string, action: "dismiss" | "restore",
           target: "fit" | "health") => void;
  hintBusy: boolean;
  togglePending: boolean;
  pendingDelete: "archive" | "purge" | null;
  prefill: { pamt: string; pcad: string; pdue: string };
  cadences: [string, string][];
}) {
  const { disabled } = b;
  // the row knows its own pool — no need to thread the group's shape down
  const isAnnualEnv = isEnv && b.period_months === 12;
  // cycle position from the row's cadence value (the label is the fallback)
  const cadVal = b.cadence_value || cadenceFromLabel(b.cadenceLabel);
  const daysTo = !isEnv && b.due_on ? daysUntil(b.due_on) : null;
  const cyc = cycleDays(cadVal);
  const settled = b.occurrences > 0 && b.paid >= b.occurrences;
  const overdue = !settled && daysTo !== null && daysTo < 0;
  const soon = !settled && daysTo !== null && daysTo >= 0 && daysTo <= SOON_DAYS;
  const frac = daysTo === null || cyc <= 0 ? 0
    : Math.min(1, Math.max(0, 1 - daysTo / cyc));
  return (
    <>
      {/* .billrow(.env) + c-* cell names anchor the <=620px grid restack in
          index.css — envelope rows have their own column set, so their own
          grid template */}
      <tr className={isEnv ? "billrow env" : "billrow"}
          id={`bill-${b.payee}`}>
        <td className="c-name"
            style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          <Link to={`/bills/history?payee=${encodeURIComponent(b.payee)}`}>
            {b.payee}</Link></td>
        <td className="num c-amt">{money(b.amount + (b.fee_total || 0))}
          {b.fee_total ? (
            <span className="mut" style={{ fontSize: 11, marginLeft: 4 }}
                  title={b.companions.map((c) =>
                    `${c.tokens} ${money(c.amount)} (within ${c.window_days}d)`)
                    .join(" · ")}>
              incl. {money(b.fee_total)} fee</span>) : null}
        </td>
        {isEnv ? (
          // the pool is stated once in Amount; this cell shows PROGRESS
          // against it, in the money map's language, instead of repeating
          // "$412 of $600" beside a column that already said $600
          <td className="c-used">
            <span className="envbar" title={`${money(b.period_used ?? b.used ?? 0)} of ${money(b.amount)}`}>
              <i className={(b.overflow ?? 0) > 0.005 ? "over" : ""}
                 style={{ width: `${Math.min(100, b.amount > 0
                   ? (100 * (b.period_used ?? b.used ?? 0)) / b.amount : 0)}%` }} />
            </span>
          </td>
        ) : (
          <td className="mut c-due" style={{ whiteSpace: "nowrap" }}>
            {b.due_on ? mmdd(b.due_on) : "\u00b7"}</td>
        )}
        {!isEnv && (
          // the bar says where you are in the cycle; the words beside it say
          // what the right edge IS — "in 12 days", "due today", "3 days late"
          // — in the bar's own colour, because the bar alone is not
          // intuitive. Amber inside SOON_DAYS, red once overdue.
          <td className="c-cycle">
            <span className="cycle-cell">
              <span className="envbar"
                    title={daysTo === null ? undefined
                      : `${Math.round(frac * 100)}% through the cycle`}>
                <i className={overdue ? "over" : soon ? "soon" : ""}
                   style={{ width: `${frac * 100}%` }} />
              </span>
              {daysTo !== null && !settled ? (
                <span className={`cycle-words ${overdue ? "r" : soon ? "a" : "g"}`}>
                  {dueText(daysTo, b.income)}</span>
              ) : null}
            </span>
          </td>
        )}
        {/* ONE status column, not State plus Health: a Health column
            prints a ✓ pill on every healthy row, making the loudest thing
            on the page everything being fine. A clean row IS the ✓; only a
            bill wanting a decision says anything. "off-ledger" is not a
            fault (cash and manual bills live there), so it lives in the
            edit expander. */}
        <td className="c-state" style={{ whiteSpace: "nowrap", overflow: "hidden" }}>
          {disabled ? <span className="pill m">disabled</span>
            : isEnv ? (
              (b.overflow ?? 0) > 0.005
                ? <span className="pill r">over +{money(b.overflow!)}</span>
                : (b.period_left ?? b.amount - (b.used ?? 0)) <= b.amount * 0.001
                  ? <span className="pill a">full</span>
                  : <span className="sub">
                      {money(b.period_left ?? b.amount - (b.used ?? 0))} left
                      {isAnnualEnv ? " this year" : ""}</span>
            ) : b.income
              ? (settled
                  ? <span className="sub">received ✓{b.paid > 1
                      ? ` \u00d7${b.paid}` : ""}</span>
                  : daysTo !== null
                    // the Cycle column already says this on desktop; the
                    // copy here is for phone width, where Cycle is hidden
                    ? <span className="sub cycle-dup">{dueText(daysTo, true)}</span>
                    // dateless but unsettled must still say so \u2014 a blank
                    // status cell is reserved for "nothing to decide"
                    : <span className="pill m"
                        title="expected, but no date on file">awaiting</span>)
            : settled
              ? <span className="sub">paid ✓</span>
              : overdue
                ? <span className="pill r">overdue</span>
                : daysTo !== null
                  ? <span className="sub cycle-dup">{dueText(daysTo)}</span>
                  // same rule for bills: unpaid with no due date is a state,
                  // not silence
                  : <span className="pill m"
                      title="unpaid, but no due date on file">awaiting</span>}
          {!isEnv && b.cap ? <>{" "}<span className="pill m">cap {b.cap}</span></> : null}
          {/* pin-to-Today is set in the edit expander; without a mark on
              the row the only way to learn which bills are on the Today
              page is to open every ✎ */}
          {!b.income && b.show_today ? <>{" "}<span className="pill m"
            title="pinned — shows as a card on the Today page and in the daily email">📌</span></> : null}
          {/* a real problem, next to the state it contradicts — dismissible;
              the flag returns only if the health changes to a new problem */}
          {!disabled && !b.health_dismissed
            && ["drifting", "stale", "mismatch", "misdated"]
              .includes(b.health ?? "") && (
            <>
              {" "}{hpill(b.health, b.last_seen)}
              {mayEdit && (
              <button title="dismiss this flag — it returns only if the problem changes"
                      style={FLAGBTN} disabled={hintBusy}
                      onClick={() => onHint(b.payee, "dismiss", "health")}>✕</button>
              )}
            </>
          )}
          {/* what the ledger actually charged — the fact that makes
              "mismatch" actionable. A hover title does not exist on a phone
              at all, so it prints beside the pill here, as it does on
              mobile. */}
          {!disabled && !b.health_dismissed && b.health === "mismatch"
            && b.last_seen && (
            <div className="sub">
              ledger {money(b.last_seen.amount)} on {mmdd(b.last_seen.date)}
            </div>
          )}
          {/* a dismissed flag is still a hidden problem, so say so and give
              the click back — dismissing was otherwise one-way */}
          {!disabled && b.health_dismissed && mayEdit && (
            <>
              {" "}
              <button title={`flag hidden (${b.health}) — show it again`}
                      aria-label={`Restore the hidden ${b.health} flag on ${b.payee}`}
                      style={FLAGBTN} disabled={hintBusy}
                      onClick={() => onHint(b.payee, "restore", "health")}>↺</button>
            </>
          )}
          {b.fit && !b.hint_dismissed &&
           (["mismatch", "stale", "misdated"].includes(b.health ?? "") || b.fit_diverges) && (
            <>
              {" "}
              {/* icon-only — the fit text is in the tooltip, which is
                  also the accessible name */}
              <Link className="pill m" style={{ textDecoration: "none" }}
                    to={`/bills/history?payee=${encodeURIComponent(b.payee)}`}
                    aria-label={`Ledger says ${b.fit.label} — differs from this bill; open its history`}
                    title={`Ledger says ${b.fit.label} — differs from this bill; click to investigate / apply`}>
                💡</Link>
              {mayEdit && (
              <button title="dismiss this hint — it returns only if the ledger fit changes"
                      style={FLAGBTN} disabled={hintBusy}
                      onClick={() => onHint(b.payee, "dismiss", "fit")}>✕</button>
              )}
            </>
          )}
        </td>
        {/* no row-level disable button: the expander this ✎ opens carries
            disable/archive/delete, and a row-level copy would be a second
            door to one of them */}
        <td className="c-act" style={{ whiteSpace: "nowrap", textAlign: "right" }}>
          {mayEdit && (
          <button className="row-menu" aria-expanded={editing}
                  aria-label={`edit ${b.payee}`}
                  style={editing ? { color: "var(--blue)" } : undefined}
                  onClick={() => onEdit(editing ? null : b.payee)}>✎</button>
          )}
        </td>
      </tr>
      {editing && (
        <EditRow d={b} cadences={cadences} cols={cols} prefill={prefill}
                 togglePending={togglePending} pendingDelete={pendingDelete}
                 onCancel={() => onEdit(null)} onSaved={onSaved}
                 onToggle={() => onToggle(b.payee)}
                 onArchive={() => onArchive(b.payee)}
                 onPurge={() => {
                   if (confirm("Permanently delete this bill? No archive copy is " +
                               "kept (ledger transactions are untouched)."))
                     onPurge(b.payee);
                 }} />
      )}
    </>
  );
}

// Manual add: create a bill by hand — for anything the
// detector won't find, including unusual cadences. "Custom…" builds an
// arbitrary FREQ:interval (every X months / years); the engine already
// schedules + budgets any interval (budget._occurrences_in_month honors it).
function AddBillForm({ onCancel, onSaved }:
                     { onCancel: () => void; onSaved: () => void }) {
  const [payee, setPayee] = useState("");
  const [amount, setAmount] = useState("");
  // pick from the ledger's real (canonical) merchants so the bill's match key
  // aligns with existing txns and matches future ones. Still free-text —
  // a brand-new merchant not yet in the ledger can be typed.
  const merchants = useQuery({ queryKey: ["merchants"], queryFn: api.merchants });
  const [mode, setMode] = useState("MONTHLY:1");   // a GROUPS value or "CUSTOM"
  const [customN, setCustomN] = useState("2");
  const [customUnit, setCustomUnit] = useState("YEARLY");
  const [nextDue, setNextDue] = useState("");
  const [income, setIncome] = useState(false);
  const [pinned, setPinned] = useState(false);
  const [category, setCategory] = useState("");
  const cats = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const [err, setErr] = useState<string | null>(null);
  const cadence = mode === "CUSTOM"
    ? `${customUnit}:${Math.max(1, parseInt(customN, 10) || 1)}`
    : mode;
  const isEnv = cadence.startsWith("ENVELOPE");
  const save = useMutation({
    mutationFn: () => api.billsSave({
      payee: payee.trim(), amount, cadence,
      next_due: nextDue, income, merchant: payee.trim(),
      ...(income ? {} : { show_today: pinned }),
      // an envelope with a category counts that category's spend toward the
      // pool — the natural read of picking one on a pool that has no
      // merchant history yet
      ...(isEnv && category.trim()
        ? { category: category.trim(), match_category: true } : {}),
    }),
    onSuccess: () => onSaved(),
    onError: (e) => setErr(errText(e)),
  });
  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <form style={{ display: "flex", gap: ".6rem", alignItems: "center",
                     flexWrap: "wrap" }}
            onSubmit={(e) => {
              e.preventDefault();
              if (payee.trim() && amount) save.mutate();
            }}>
        <input type="text" required list="known-merchants"
               placeholder="merchant — pick a known one or type a new name"
               title="pick an existing merchant so this bill matches its past + future transactions"
               value={payee} style={{ width: "18rem" }}
               onChange={(e) => setPayee(e.target.value)} />
        <datalist id="known-merchants">
          {(merchants.data?.merchants ?? []).map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
        <label className="mut">$ <input type="number" step="0.01" min="0"
               required placeholder="0.00" value={amount} style={{ width: "7rem" }}
               onChange={(e) => setAmount(e.target.value)} /></label>
        <select value={mode} onChange={(e) => setMode(e.target.value)}>
          {SIMPLE_CADENCES.map(([v, label]) => (
            <option key={v} value={v}>{label}</option>
          ))}
          <option value="CUSTOM">Custom interval…</option>
        </select>
        {mode === "CUSTOM" && (
          <label className="mut">every <input type="number" min="1"
                 value={customN} style={{ width: "3.5rem" }}
                 onChange={(e) => setCustomN(e.target.value)} />
            <select value={customUnit}
                    onChange={(e) => setCustomUnit(e.target.value)}>
              <option value="MONTHLY">month(s)</option>
              <option value="YEARLY">year(s)</option>
            </select></label>
        )}
        <label className="mut">next due <input type="date" value={nextDue}
               onChange={(e) => setNextDue(e.target.value)} /></label>
        {isEnv && (
          <label className="mut"
                 title="spend in this category counts toward the pool even when the merchant doesn't match — for pools fed by many merchants (a vet, a groomer, a pet store)">
            category <input type="text" list="add-bill-categories"
                   value={category} placeholder="none" style={{ width: "9rem" }}
                   onChange={(e) => setCategory(e.target.value)} />
            <datalist id="add-bill-categories">
              {(cats.data?.categories ?? []).map((c) => (
                <option key={c} value={c} />
              ))}
            </datalist>
          </label>
        )}
        <label className="mut"><input type="checkbox" checked={income}
               onChange={(e) => setIncome(e.target.checked)} /> income</label>
        {!income && (
          <label className="mut"
                 title="show this bill as a card on the Today page and in the daily email">
            <input type="checkbox" checked={pinned}
                   onChange={(e) => setPinned(e.target.checked)} /> pin to Today
          </label>
        )}
        <button className="pri" disabled={save.isPending}>add bill</button>
        <button type="button" className="btn" onClick={onCancel}>cancel</button>
      </form>
      {err && <div className="neg" style={{ fontSize: 13, marginTop: ".4rem" }}>{err}</div>}
    </div>
  );
}

function EditRow({ d, cadences, cols, prefill, togglePending, pendingDelete,
                   onCancel, onSaved, onToggle, onArchive, onPurge }: {
  d: Bill; cadences: [string, string][]; cols: number;
  prefill: { pamt: string; pcad: string; pdue: string };
  togglePending: boolean; pendingDelete: "archive" | "purge" | null;
  onCancel: () => void; onSaved: () => void;
  onToggle: () => void;
  onArchive: () => void; onPurge: () => void;
}) {
  const [payee, setPayee] = useState(d.payee);
  const [amount, setAmount] = useState(prefill.pamt || d.amount.toFixed(2));
  const [cadence, setCadence] = useState(prefill.pcad || d.cadence_value);
  const [nextDue, setNextDue] = useState(prefill.pdue || d.due_on || "");
  const [cap, setCap] = useState(d.cap === null ? "" : String(d.cap));
  const [pinned, setPinned] = useState(d.show_today);
  const [matchCat, setMatchCat] = useState(d.match_category);
  const [category, setCategory] = useState(d.category ?? "");
  // the transaction category stamped on matched charges — a merchant
  // that is several things (a gym's membership beside its café)
  const [txnCat, setTxnCat] = useState(d.txn_category ?? "");
  // the fees that ride with this payment — edited as rows of
  // (words on the charge, amount, days either side of the main charge)
  const feeRows = (d.companions ?? []).map((c) => ({ tokens: c.tokens,
    amount: String(c.amount), window_days: String(c.window_days) }));
  const [fees, setFees] = useState(feeRows);
  const feesDirty = JSON.stringify(fees) !== JSON.stringify(feeRows);
  // the merchants this bill pays — its identity. The ledger's rows under
  // these names are the bill's charges, whatever the bank's wording; a
  // rename or merge the nightly pass notices arrives as a proposal above.
  const [merchants, setMerchants] = useState<string[]>(d.merchants ?? []);
  const [newMerchant, setNewMerchant] = useState("");
  const merchantsDirty =
    JSON.stringify(merchants) !== JSON.stringify(d.merchants ?? []);
  const known = useQuery({ queryKey: ["merchants"], queryFn: api.merchants });
  const addMerchant = () => {
    const v = newMerchant.trim();
    if (v && !merchants.includes(v) && merchants.length < 20)
      setMerchants([...merchants, v]);
    setNewMerchant("");
  };
  const cats = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const isEnv = cadence.startsWith("ENVELOPE");
  const [err, setErr] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => api.billsSave({
      payee, amount, cadence, next_due: nextDue, income: d.income,
      orig_payee: d.payee, ...(cadence !== "ENVELOPE:1" ? { cap } : {}),
      ...(d.income ? {} : { show_today: pinned }),
      // only sent when edited, so a save from elsewhere keeps the fees
      ...(feesDirty ? { companions: fees
          .filter((f) => f.tokens.trim() && f.amount.trim())
          .map((f) => ({ tokens: f.tokens.trim(), amount: Number(f.amount),
                         window_days: Number(f.window_days || 3) })) } : {}),
      // category + its pool flag only travel on envelope saves, so an
      // occurrence edit can never strip detection's category from the row
      ...(isEnv ? { match_category: matchCat && !!category.trim(),
                    category: category.trim() || null } : {}),
      ...(txnCat !== (d.txn_category ?? "") ? { txn_category: txnCat } : {}),
      // only sent when edited, so a save from elsewhere keeps the identity
      ...(merchantsDirty ? { merchants } : {}),
    }),
    onSuccess: () => onSaved(),
    onError: (e) => setErr(errText(e)),
  });
  return (
    <tr className="editrow">
      <td colSpan={cols} style={{ background: "var(--hover)" }}>
        <form style={{ display: "flex", gap: ".5rem", alignItems: "center",
                       flexWrap: "wrap", padding: ".2rem 0" }}
              onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
          <input type="text" required value={payee} style={{ width: "16rem" }}
                 title="renaming changes ledger matching (first significant word of the name)"
                 onChange={(e) => setPayee(e.target.value)} />
          <label className="mut">$ <input type="number" step="0.01" min="0"
                 value={amount} style={{ width: "6.5rem" }}
                 onChange={(e) => setAmount(e.target.value)} /></label>
          <select value={cadence} onChange={(e) => setCadence(e.target.value)}>
            {cadences.map(([v, label]) => (
              <option key={v} value={v}>{label}</option>
            ))}
          </select>
          <label className="mut">next due <input type="date" value={nextDue}
                 onChange={(e) => setNextDue(e.target.value)} /></label>
          {cadence !== "ENVELOPE:1" && (
            <label className="mut"
                   title="max occurrences counted per month (blank = no cap)">
              occ cap <input type="number" min="1" value={cap}
                     style={{ width: "2.7rem" }}
                     onChange={(e) => setCap(e.target.value)} /></label>
          )}
          {isEnv && (
            <>
              <label className="mut">category{" "}
                <input type="text" list="bill-categories" value={category}
                       placeholder="none" style={{ width: "9rem" }}
                       onChange={(e) => {
                         const v = e.target.value;
                         setCategory(v);
                         // "count all X spending" was granted for one
                         // specific category — a different one starts
                         // unchecked instead of inheriting the grant;
                         // retyping the original restores its saved state
                         setMatchCat(v.trim() === (d.category ?? "").trim()
                           ? d.match_category : false);
                       }} />
              </label>
              <datalist id="bill-categories">
                {(cats.data?.categories ?? []).map((c) => (
                  <option key={c} value={c} />
                ))}
              </datalist>
              <label className="mut"
                     title="spend in this category counts toward the pool even when the merchant doesn't match — for pools fed by many merchants (a vet, a groomer, a pet store)">
                <input type="checkbox" checked={matchCat && !!category.trim()}
                       disabled={!category.trim()}
                       onChange={(e) => setMatchCat(e.target.checked)} />
                {" "}count all {category.trim() || "category"} spending
              </label>
            </>
          )}
          {!d.income && (
            <label className="mut"
                   title="show this bill as a card on the Today page and in the daily email">
              <input type="checkbox" checked={pinned}
                     onChange={(e) => setPinned(e.target.checked)} />
              {" "}pin to Today
            </label>
          )}
          <div className="mut" style={{ width: "100%" }}
               title="the merchants this bill pays — its charges are the ledger's rows under these names, whatever the bank's wording. Empty = match by the words of the bill's name.">
            <span>Merchants</span>
            {merchants.map((m) => (
              <span key={m} className="pill m" style={{ marginLeft: ".35rem" }}>
                {m}{" "}
                <button type="button" className="linklike" title="remove"
                        onClick={() => setMerchants(merchants.filter((x) => x !== m))}>
                  ×</button>
              </span>
            ))}
            {merchants.length < 20 && (
              <>
                <input type="text" list="bill-merchants" value={newMerchant}
                       placeholder="add a merchant"
                       style={{ width: "12rem", marginLeft: ".35rem" }}
                       onChange={(e) => setNewMerchant(e.target.value)}
                       onKeyDown={(e) => {
                         if (e.key === "Enter") { e.preventDefault(); addMerchant(); }
                       }} />
                <button type="button" className="linklike" onClick={addMerchant}>
                  add</button>
                <datalist id="bill-merchants">
                  {(known.data?.merchants ?? []).map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
              </>
            )}
          </div>
          {!d.income && !isEnv && (
            <div className="mut" style={{ width: "100%" }}
                 title="a fee that rides with this payment — a processor's convenience fee, a portal's service charge. Landing within the window of the main charge it counts as part of the same occurrence, is never proposed as a bill of its own, and shows here as 'incl. fee'.">
              <span>Fees &amp; add-ons</span>
              {fees.map((f, i) => (
                <div key={i} style={{ display: "flex", gap: ".35rem",
                                      alignItems: "center", marginTop: ".25rem" }}>
                  <input value={f.tokens} placeholder="words on the charge"
                         style={{ width: "12rem" }}
                         onChange={(e) => setFees(fees.map((x, j) =>
                           j === i ? { ...x, tokens: e.target.value } : x))} />
                  <input value={f.amount} placeholder="$" inputMode="decimal"
                         style={{ width: "4.5rem" }}
                         onChange={(e) => setFees(fees.map((x, j) =>
                           j === i ? { ...x, amount: e.target.value } : x))} />
                  <span>within</span>
                  <input value={f.window_days} inputMode="numeric"
                         style={{ width: "2.5rem" }}
                         onChange={(e) => setFees(fees.map((x, j) =>
                           j === i ? { ...x, window_days: e.target.value } : x))} />
                  <span>days</span>
                  <button type="button" className="linklike"
                          onClick={() => setFees(fees.filter((_, j) => j !== i))}>
                    remove</button>
                </div>
              ))}
              {fees.length < 8 && (
                <button type="button" className="linklike"
                        style={{ marginTop: ".25rem" }}
                        onClick={() => setFees([...fees,
                          { tokens: "", amount: "", window_days: "3" }])}>
                  + add a fee</button>
              )}
            </div>
          )}
          {!d.income && (
            <label className="mut"
                   title="every charge this bill matches is categorized as this — for a merchant that is several things (a gym's membership beside its café). Other charges at the merchant keep their own category; a category you set on a row yourself always wins.">
              matched charges are{" "}
              <select value={txnCat} style={{ maxWidth: "12rem" }}
                      onChange={(e) => setTxnCat(e.target.value)}>
                <option value="">— merchant's category —</option>
                {[...new Set([...(cats.data?.categories ?? []),
                              ...(cats.data?.plaid_spend ?? [])])]
                  // flow (transfer / income / loan payment) is decided by
                  // the ledger's own machinery, never by a bill — the
                  // server refuses these, so don't offer them
                  .filter((c) => !(cats.data?.plaid_flow ?? []).includes(c))
                  .map((c) => (
                  <option key={c} value={c}>{catLabel(c)}</option>
                ))}
              </select>
            </label>
          )}
          {d.income && <span className="pill m">income</span>}
          <button className="pri" disabled={save.isPending}>save</button>
          <button type="button" className="btn" onClick={onCancel}>cancel</button>
        </form>
        {err && <div className="neg" style={{ fontSize: 13 }}>{err}</div>}
        {/* one destructive write at a time: while any of these is in
            flight all three lock, or a slow request invites a second click
            (double purge; double toggle silently nets to no change) */}
        <div style={{ marginTop: ".3rem", display: "flex", gap: ".4rem", flexWrap: "wrap" }}>
          <button title="moves to Archived below; restorable"
                  disabled={togglePending || pendingDelete !== null}
                  onClick={onArchive}>
            {pendingDelete === "archive" ? "archiving…" : "archive bill"}</button>
          <button disabled={togglePending || pendingDelete !== null}
                  onClick={onPurge}>
            {pendingDelete === "purge" ? "deleting…" : "delete forever"}</button>
          <button disabled={togglePending || pendingDelete !== null}
                  onClick={onToggle}>
            {togglePending ? "working…"
              : `${d.disabled ? "enable" : "disable"} (excluded from budget while disabled)`}</button>
        </div>
      </td>
    </tr>
  );
}

function UpcomingCalendar() {
  const q = useQuery({ queryKey: ["calendar"], queryFn: () => api.calendar(35) });
  // a fetch that never resolves would render as nothing at all — the
  // section owns its own query, so it owns saying so too (mobile parity)
  if (q.isPending || q.isError) {
    return (
      <div className="card">
        <h2>Next five weeks</h2>
        {q.isPending ? <p className="mut">loading…</p> : (
          <p className="mut">Couldn't load the calendar —{" "}
            <button className="linklike" style={LINKLIKE}
                    onClick={() => q.refetch()}>retry</button>
          </p>
        )}
      </div>
    );
  }
  const byDate = new Map(q.data.calendar.map((d) => [d.date, d]));

  // 5 ISO weeks starting Monday of the current week
  const start = new Date(q.data.start + "T00:00:00");
  const monday = new Date(start);
  monday.setDate(start.getDate() - ((start.getDay() + 6) % 7));
  const cells: Date[] = Array.from({ length: 35 }, (_, i) => {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    return d;
  });
  const iso = (d: Date) =>
    `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  // browser-local today. The server's start is the HOUSEHOLD's day, not
  // UTC-today — UTC would highlight tomorrow for a US evening user — and
  // the device clock still wins for a travelling user. Events before
  // local-today stay dimmed either way.
  const todayIso = iso(new Date());

  // The grid IS the agenda: each day carries its total and labels, and
  // the full list sits in the cell's tooltip. The per-date cards that
  // used to follow it repeated the same events in a second shape and
  // pushed the Archived table off the bottom of the page.
  return (
    <div className="card">
      <h2>Next five weeks</h2>
      <div className="cal-grid">
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => (
          <div key={d} className="cal-head">{d}</div>
        ))}
        {cells.map((d) => {
          const key = iso(d);
          const day = byDate.get(key);
          const past = key < todayIso;
          return (
            <div key={key}
                 className={"cal-cell" + (key === todayIso ? " today" : "") +
                            (past ? " past" : "")}
                 title={day?.events.map((e) =>
                   `${e.label} ${money(Math.abs(e.amount))}`).join("\n")}>
              <span className="cal-day">{d.getDate()}</span>
              {day && !past && (
                <span className={"cal-amt " + (day.total > 0 ? "in" : "out")}>
                  {money(Math.abs(day.total))}
                </span>
              )}
              {day && !past && day.events.slice(0, 2).map((e, i) => (
                <span key={i} className="cal-label">{e.label}</span>
              ))}
              {day && !past && day.events.length > 2 && (
                <span className="cal-label mut">+{day.events.length - 2} more</span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// One line of the triage queue: severity dot, the story the ledger tells,
// and the ONE action the server's evidence supports. The story is built
// from fields the row already carries (last_match, cycle_days, last_seen,
// suggestion) — the client never invents an action or a number.
function AttentionLine({ b, mayEdit, busy, onJump, onAccept, onDisable,
                         onDismiss }: {
  b: Bill; mayEdit: boolean; busy: boolean; onJump: () => void;
  onAccept: (amt: number) => void; onDisable: () => void;
  onDismiss: () => void;
}) {
  const s = b.suggestion;
  const plusDays = (iso: string, d: number) => {
    const t = new Date(iso + "T00:00:00Z");
    t.setUTCDate(t.getUTCDate() + d);
    return t.toISOString().slice(0, 10);
  };
  // the stale verdict is judged on ANY charge from the merchant, so the
  // story's "nothing since" must be the newest one — an off-amount charge
  // newer than the last exact match is still activity
  const lastSeen = b.last_seen?.date
    && (!b.last_match || b.last_seen.date > b.last_match)
    ? b.last_seen.date : b.last_match;
  const story =
    b.health === "stale"
      ? `missed its${lastSeen && b.cycle_days
          ? ` ~${mmdd(plusDays(lastSeen, b.cycle_days))}` : ""} cycle — ` +
        (lastSeen ? `nothing since ${mmdd(lastSeen)}`
                  : "no matching charge on record")
    : b.health === "mismatch"
      ? (s?.action === "accept_amount" && s.amount !== undefined
          ? `charges now run ${money(s.amount)}, bill says ${money(b.amount)}`
          : `charges near ${b.last_seen ? money(b.last_seen.amount) : "?"}` +
            `${b.last_seen ? ` on ${mmdd(b.last_seen.date)}` : ""}, ` +
            `bill says ${money(b.amount)} — amounts vary`)
    : b.health === "drifting"
      ? "amount wandering — auto-corrects tonight"
      : "payments match, but the due-date anchor is off the real draft day";
  return (
    <div style={{ display: "flex", gap: ".7rem", alignItems: "center",
                  padding: ".45rem .7rem",
                  borderBottom: "1px solid var(--line)", fontSize: 13 }}>
      <span style={{ width: 8, height: 8, borderRadius: "50%", flex: "none",
                     background: b.health === "stale"
                       ? "var(--bad, #f85149)" : "var(--warn, #d29922)" }} />
      <button className="linklike" onClick={onJump}
              style={{ ...LINKLIKE, flex: 1, textAlign: "left",
                       fontSize: 13 }}>
        <b>{b.payee}</b> {story}
      </button>
      {/* a viewer sees the story (it is information) but no actions —
          the same gate every other write control on this page uses */}
      {mayEdit && s?.action === "accept_amount" && s.amount !== undefined && (
        <button className="pri m" disabled={busy}
                onClick={() => onAccept(s.amount!)}>
          Accept {money(s.amount)}</button>
      )}
      {mayEdit && s?.action === "disable" && (
        <button className="pri m" disabled={busy} onClick={onDisable}
                title="keeps the bill and its history; just leaves the budget">
          Disable bill</button>
      )}
      {mayEdit && (!s || s.action === "review") && (
        <button className="m" onClick={onJump}>Review…</button>
      )}
      {mayEdit && (
        <button className="m" disabled={busy} onClick={onDismiss}
                title="hidden until its status changes">Dismiss</button>
      )}
    </div>
  );
}
