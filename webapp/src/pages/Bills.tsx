// Bills — the schedule's monthly load as the headline, then proposals, then
// ONE table for every cadence (thin group separators, envelopes apart because
// their columns differ), inline edit rows, and the 5-week calendar.
// Status is ONE column, not a state pill beside a health pill: a healthy
// bill says nothing, so the only coloured rows want a decision.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, Fragment, useContext, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { api, errText, mmdd, money as money$, toast, type Bill, type BillsData, type CalendarData, type Settings, catLabel, proposalSuperseded } from "../api/client";
import { patchList, patchQuery, removeFromList, SETTINGS_SAVES, saveSettings } from "../api/cache";
import { canEdit, isOwner } from "../role";
import { FailedLine, StaleLine } from "../components/Loaded";
import { approveAllWords, EMPTY_LINES, householdDay,
         scanAlreadyRunning, SCAN_RUNNING_RETRY_MS } from "../moneymath";
import EmptyState from "../components/EmptyState";
import { ADD_CADENCES, CUSTOM_UNITS, cadenceChoices, customCadence, customMax }
  from "../cadence";
import { TWICE_A_MONTH, cadenceFields, cadencePick, onSeveralDays, withSeveralDays }
  from "../monthdays";
import Note from "../components/Note";
import { scrollBehavior } from "../motion";
const money = (v: number | null | undefined) => money$(v, true);  // cents on transaction/recurring surfaces

// ---- stepping back through the months ----
// The page's stats are one month's: this month as of today, or a past
// month as it ended (?month=YYYY-MM, the server's own reading). The rows
// learn which from this context rather than a prop threaded through the
// table: a past month has no "in 12 days" — a bill was paid, missed, or
// not due.
const PastMonthCtx = createContext<string | null>(null);   // the label, when past
const ymOf = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
export const shiftYm = (ym: string, by: number) => {
  const [y, m] = ym.split("-").map(Number);
  const d = new Date(y, m - 1 + by, 1);
  return ymOf(d);
};
export const ymLabel = (ym: string) => {
  const [y, m] = ym.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
};


// cadence select value → group heading (the list order is the rank)
const GROUPS: [string, string][] = [
  ["WEEKLY:1", "Weekly"], ["DAILY:14", "Every 14 days"],
  ["WEEKLY:2", "Every 2 weeks"], [TWICE_A_MONTH, "Twice a month"],
  ["MONTHLY:1", "Monthly"],
  ["ENVELOPE:1", "Monthly envelope"], ["ENVELOPE:12", "Annual envelope"],
  ["WEEKLY:8", "Every 8 weeks"],
  ["MONTHLY:2", "Every 2 months"], ["MONTHLY:3", "Every 3 months"],
  ["MONTHLY:6", "Every 6 months"], ["YEARLY:1", "Yearly"],
  ["YEARLY:2", "Every 2 years"],
  ["ONE_TIME:1", "One-time / other"],
];
const groupOf = (cadence: string) =>
  GROUPS.find(([v]) => v === cadence)?.[1] ?? "One-time / other";

// The ADD pickers stay short: the common cadences, one-time and the
// envelopes; anything unusual (every 2 weeks/14 days/3 months…) is one
// "Custom interval…" click. GROUPS above still labels + groups every cadence,
// and inline EDIT keeps the full server list plus the bill's own interval
// when that list lacks it, so an existing custom bill can always keep it.
// The list lives in ../cadence so the phone's copy is checked against it.
export const SIMPLE_CADENCES: [string, string][] = ADD_CADENCES;
const groupRank = (name: string) => {
  const i = GROUPS.findIndex(([, g]) => g === name);
  return i === -1 ? 90 : i;
};

interface Row extends Bill {
  cadenceLabel: string;
}

// The row's due date, countdown, its colour and the cycle bar's fill are
// the server's (api_bills.due_words), counted from the household's day —
// the day the server, Today and the mail judge a due date by. The cycle
// bar fills as the due date approaches, in the envelopes' own language.
// the server refuses a fee's words past this length (POST /api/bills/save)
const FEE_WORDS_MAX = 80;

const hpill = (h: string | null,
               seen?: { date: string; amount: number } | null,
               withdrawnOn?: string | null) =>
  h === "ok" ? <span className="pill g hide-m" title="ledger matches">✓</span>
  // the missed cycle WAS charged; the bank feed took the charge back — a
  // warning to check, not a bill gone quiet
  : h === "withdrawn" ? <span className="pill a"
        title={`charge withdrawn by the bank feed${withdrawnOn ? ` (${mmdd(withdrawnOn)})` : ""}`}>withdrawn</span>
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

// The nightly finder's on/off. Choosing "I'll enter them myself" in the
// wizard turns it off; this is the one place to turn it back on, so it
// sits beside the button that runs the same search by hand.
function NightlyFinderSwitch() {
  const qc = useQueryClient();
  const s = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const save = useMutation({
    scope: SETTINGS_SAVES,
    mutationFn: (on: boolean) => saveSettings(qc, { bill_detection_auto: on }),
    // the box flips on the click; saveSettings then seeds the cache with
    // the server's full view, and a failed write refetches the stored one
    onMutate: (on) => patchQuery<Settings>(qc, ["settings"],
                                           { bill_detection_auto: on }),
    onError: (e) => {
      toast(errText(e));
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
  // the switch is the household's, and the server refuses anyone but the
  // owner a change — a member or viewer reads where it stands instead of
  // flipping a box that only answers 403
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  // absent or null means on — only an explicit false stops it
  // An unread setting is neither on nor off: nothing while it loads, the
  // failed line when it failed — never a box that reads "on" by default.
  if (s.data === undefined)
    return s.isError ? <FailedLine q={s} what="the nightly search setting" />
      : null;
  const on = s.data.bill_detection_auto !== false;
  if (!isOwner(me))
    return !s.data ? null : (
      <div className="sub" style={{ fontSize: 12, marginTop: ".45rem" }}>
        Nightly search for new bills and paychecks:{" "}
        <b style={{ color: "var(--ink)" }}>{on ? "on" : "off"}</b> — the
        household owner sets this.</div>
    );
  return (
    <div style={{ marginTop: ".45rem" }}>
      <label style={{ display: "inline-flex", gap: ".4rem",
                      alignItems: "center", cursor: "pointer" }}>
        <input type="checkbox" checked={on}
               disabled={!s.data || save.isPending}
               onChange={(e) => save.mutate(e.target.checked)} />
        Look for new bills and paychecks every night
      </label>
      {/* what "off" means is said only while it is off */}
      {s.data && !on && (
      <div className="sub" style={{ fontSize: 12, marginTop: ".1rem" }}>
        Off: the nightly search proposes nothing new; drift, due dates and
        change/stopped offers for your existing bills still run, and Find
        bills &amp; income always works.</div>)}
    </div>
  );
}

// autoDetect (wizard): run the finder on mount so the step opens with
// proposals already on screen — confirm, don't hunt for a button
export default function Bills({ autoDetect = false }:
                                   { autoDetect?: boolean } = {}) {
  const qc = useQueryClient();
  const [sp, setSp] = useSearchParams();
  const edit = sp.get("edit") ?? "";
  // the month on view: absent = this month (the live key every write
  // patches); a past month has its own key, so stepping back never
  // overwrites the live list in the cache
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  // "this month" is the household's — the month GET /api/bills answers
  // for — so ‹ reaches the month that just closed there, not the device's
  const curYm = ymOf(householdDay(new Date(), me.data?.timezone));
  const month = sp.get("month") ?? "";
  const isPast = !!month && month < curYm;
  const q = useQuery({
    queryKey: isPast ? ["bills", "month", month] : ["bills"],
    queryFn: () => api.bills(isPast ? month : undefined),
  });
  const setMonth = (ym: string) => setSp((prev) => {
    const n = new URLSearchParams(prev);
    if (ym >= curYm) n.delete("month"); else n.set("month", ym);
    n.delete("edit");
    return n;
  }, { replace: true });
  // the calendar section mounts once the bills are in; its own request
  // starts here, beside the bills', so the section paints with them
  useQuery({ queryKey: ["calendar"], queryFn: () => api.calendar(35) });
  // detect / approve / edit / archive are editing writes — viewers keep
  // the full read view (groups, calendar, proposals as information)
  const mayEdit = canEdit(me);
  const [adding, setAdding] = useState(false);
  // the triage queue's undo affordance: one reversible action at a time
  const [undo, setUndo] = useState<{ label: string;
                                     act: () => void } | null>(null);
  const jumpToRow = (payee: string) => {
    const el = document.getElementById(`bill-${payee}`);
    el?.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
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
    el.scrollIntoView({ behavior: scrollBehavior(), block: "center" });
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

  const dropProposal = (pid: string) =>
    qc.setQueryData(["bills"], (old?: BillsData) => old && {
      ...old,
      proposals: old.proposals.filter((p) => p.id !== pid),
    });

  const act = useMutation({
    mutationFn: ({ pid, action }: { pid: string; action: "approve" | "reject" }) =>
      api.billsProposal(pid, action),
    onSuccess: (r, v) => {
      const gone = proposalSuperseded(r);
      toast(gone ? `${r.payee} is already tracked — the offer was withdrawn`
        : `${v.action === "approve" ? "Approved" : "Rejected"}: ${r.payee}`
          + (r.note ? ` — ${r.note}` : ""));
      // drop the decided proposal from the cached list NOW: the server has
      // already committed it, and waiting for the refetch to prove it left
      // the row sitting there through every other query this triggers.
      dropProposal(v.pid);
      // approving writes one bill; rejecting (or a withdrawn offer) none
      if (v.action === "approve" && !gone) refetchAll();
      else refetchAfterReject();
    },
    onError: (e, v) => {
      toast(errText(e));
      // an offer for a bill already tracked is withdrawn by the server as
      // it refuses: the card could neither be approved nor dismissed
      if (proposalSuperseded(e)) { dropProposal(v.pid); refetchAfterReject(); }
    },
  });

  const approveAll = useMutation({
    mutationFn: api.billsApproveAll,
    onSuccess: (r) => {
      toast(approveAllWords(r)
        // an approve that cleared a pin says so, per bill
        + (r.notes?.length
          ? ` — ${r.notes.map((n) => `${n.payee}: ${n.note}`).join("; ")}` : ""));
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
    onError: (e) => {
      if (scanAlreadyRunning(e)) {
        // another pass (the nightly, another tab) is finding the bills now
        toast("A scan is already running — the list updates when it ends.");
        window.setTimeout(refetchAll, SCAN_RUNNING_RETRY_MS);
        return;
      }
      toast(errText(e));
    },
  });

  const toggle = useMutation({
    mutationFn: (v: { payee: string; disabled: boolean; fromQueue?: boolean }) =>
      api.billsToggle(v.payee, v.disabled),
    onSuccess: (r, v) => {
      toast(`${r.payee}: ${r.disabled ? "disabled (excluded from budget)" : "enabled"}`);
      // the row and its editor read `disabled` from the list — flip it from
      // the response, then refetch
      patchList<BillsData, Bill>(qc, ["bills"], "bills",
        (b) => b.payee === r.payee, { disabled: r.disabled });
      // the triage queue's Disable arms its Undo HERE, not at click time
      // (as acceptAmount does) — armed before the write is confirmed, a
      // failed disable would show "Disabled X. [Undo]" for a bill that was never
      // disabled, with a live Undo that would fire blind later
      if (v.fromQueue && v.disabled) {
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
      // amount only: no cadence, income or next_due, so the server keeps
      // the bill's stored values — this row's copy may predate an edit made
      // elsewhere, and resending its cadence or direction would revert it.
      // orig_payee marks this an edit of the bill the row shows, so a bill
      // deleted since the list loaded is refused instead of recreated
      api.billsSave({ payee: b.payee, orig_payee: b.payee, amount: amt }),
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
  // it runs. Fired on mount it would race that load: the scan writes its
  // proposals while GET /api/bills is already in flight, and the invalidate
  // that follows is folded into that in-flight request (react-query does
  // not cancel a query's very first fetch — there is no data to protect),
  // so the page would render the pre-scan snapshot — "Scan complete: 6 bills"
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

  const shownYm = isPast ? month : curYm;
  // step back through the months: the per-bill paid/received below are the
  // month on view, as it ended; the schedule stays live. The stepper stays
  // on screen while a month loads and when it fails, so the month that
  // failed is one click from the one before it.
  const stepper = !autoDetect && (
    <div className="sub" style={{ display: "flex", gap: ".5rem", alignItems: "center",
                                  flexWrap: "wrap", margin: "-.4rem 0 .8rem" }}>
      <button aria-label="Previous month"
              onClick={() => setMonth(shiftYm(shownYm, -1))}>‹</button>
      <b style={{ color: "var(--ink)" }}>{ymLabel(shownYm)}</b>
      <button aria-label="Next month" disabled={!isPast}
              onClick={() => setMonth(shiftYm(shownYm, 1))}>›</button>
      {isPast ? (
        <>
          <button onClick={() => setMonth(curYm)}>This month</button>
          <span>· how the month ended: paid and received per bill. Edits change the live schedule.</span>
        </>
      ) : null}
    </div>
  );
  const title = <h1>Bills &amp; Income</h1>;
  if (q.isPending)
    return (<>{title}{stepper}
      <div className="centered"><span className="mut">loading…</span></div></>);
  if (q.data === undefined)
    return (<>{title}{stepper}
      <FailedLine q={q} what={isPast ? `bills for ${ymLabel(shownYm)}`
                                     : "bills"} /></>);

  const { proposals, archived, cadences } = q.data;
  const rows: Row[] = bills.map((b) => ({ ...b, cadenceLabel: b.cadence }));
  // expense bills are amount<0 (bills_status); income bills — paychecks,
  // modelled as recurring rows — get their own section at the very top,
  // because they are the anchor number the bill groups below are paid from
  const incomeRows = rows.filter((r) => r.income);
  // group by cadence (fixed rank order); within a group, order by next due
  const groups = new Map<string, Row[]>();
  for (const r of rows) {
    if (r.income) continue;
    const g = r.cadence_value
      ? groupOf(cadencePick(r.cadence_value, r.by_month_day))
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
    // the row sends the state its button offered, so a list that went
    // stale under another member's change cannot flip the bill back
    onToggle: (p: string, disabled: boolean) =>
      toggle.mutate({ payee: p, disabled }),
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
               pdue: sp.get("pdue") ?? "", pdays: sp.get("pdays") ?? "" },
    cadences,
  };
  return (
    <>
    <PastMonthCtx.Provider value={isPast ? ymLabel(month) : null}>
      {title}
      {stepper}
      {/* a failed refetch says the bills below are from before it (the
          phone's line) */}
      <StaleLine query={q} />
      {/* HERO: the month in the plan's own terms — Bills, Income and what
          is left after them — as the server words them (bills_hero), so
          this page, Budget and Today say the same thing about the same
          month. Nothing here is added up on the page. */}
      {!autoDetect && (
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", gap: "1rem", alignItems: "baseline",
                      flexWrap: "wrap" }}>
          {/* the page's figures as labelled tiles, the way Net Worth
              states its own: a sentence of figures has to be read, a row
              of tiles is scanned */}
          {totals?.hero && totals.hero.length > 0 && (
            <div className={"grid herotiles cols" + Math.min(4, Math.max(2, totals.hero.length))}
                 style={{ flex: "1 1 100%", marginTop: 0, gap: ".6rem" }}>
              {totals.hero.map((t) => (
                <div key={t.key} className="tile">
                  <div className="sub">{t.label}</div>
                  <div className={"kpi sm " + t.tone}>{money$(t.amount)}</div>
                  <div className="sub">{t.note}</div>
                </div>
              ))}
            </div>
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
        {/* a viewer reads the finder's state like the rest of the page;
            only the owner gets the switch */}
        <NightlyFinderSwitch />
        <div className="sub" style={{ marginTop: ".2rem", display: "flex",
                                      gap: ".9rem", flexWrap: "wrap",
                                      alignItems: "center" }}>
          {totals?.week_text && (
            <span>Next 7 days: <b style={{ color: "var(--ink)" }}>
              {totals.week_text}</b></span>
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
                        && toggle.variables?.payee === b.payee)
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
                  toggle.mutate({ payee: b.payee, disabled: true,
                                  fromQueue: true });
                }}
                onDismiss={() => hint.mutate({ payee: b.payee,
                                               action: "dismiss",
                                               target: "health" })} />
            ))}
          </div>
        )}
        {undo && (
          <Note style={{ marginTop: ".4rem" }}>
            {undo.label}{" "}
            <button className="linklike" style={LINKLIKE}
                    disabled={toggle.isPending || acceptAmount.isPending}
                    onClick={() => { undo.act(); setUndo(null); }}>
              Undo</button>{" "}
            <button className="linklike" style={LINKLIKE} aria-label="Dismiss"
                    onClick={() => setUndo(null)}>✕</button>
          </Note>
        )}
      </div>
      )}
      {autoDetect && detect.isPending && (
        <span className="mut">⟳ scanning for bills &amp; income…</span>
      )}
      {adding && (
        <AddBillForm onCancel={() => setAdding(false)}
                     onSaved={(restored) => {
                       setAdding(false);
                       toast(restored ? "Bill restored." : "Bill added.");
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
                      onClick={() => approveAll.mutate(
                        proposals.map((p) => p.id))}>
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
                  <th className="hide-m">Evidence</th>
                    <th><span className="sr-only">Actions</span></th></tr>
            </thead>
            <tbody>
              {proposals.map((p) => (
                <tr key={p.id}>
                  <td className="c-kind" style={{ whiteSpace: "nowrap" }}>
                    {/* the server's words for what approving does ("new
                        bill", "stop tracking"); an older server sends none
                        and the row shows no pill rather than composing one */}
                    {p.change_text && (
                      <span className={"pill " + (p.kind === "remove" ? "r" : p.kind === "add" ? "g" : "a")}>
                        {p.change_text}</span>)}
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
                      <span className="mut"> — add to bill “{p.bill_payee}”</span>) : null}
                  </td>
                  <td className="num c-amt">{money(p.amount)}</td>
                  <td className="mut c-cad">
                    {/* the server's next date, never one already past */}
                    {[p.cadence_text, p.upcoming_due ? `next ${mmdd(p.upcoming_due)}` : null]
                      .filter(Boolean).join(" · ")}
                  </td>
                  <td className="mut hide-m c-ev"
                      title={[p.tag_text, p.summary].filter(Boolean).join(" · ")}
                      style={{ maxWidth: "22rem", overflow: "hidden", textOverflow: "ellipsis",
                               whiteSpace: "nowrap" }}>
                    {[p.tag_text, p.summary].filter(Boolean).join(" · ")}</td>
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
      {bills.length > 0 && !autoDetect && (isPast
        ? (q.data.month_calendar
            ? <MonthCalendar ym={month} days={q.data.month_calendar} /> : null)
        : <UpcomingCalendar />)}

      {archived && archived.length > 0 && (
        <details className="card disclosure">
          <summary className="mut">
            Archived — {archived.length} removed bill{archived.length > 1 ? "s" : ""} (history kept)
          </summary>
          {/* .bills-arch + c-*: restacked at <=620px like the live groups */}
          <table className="bills-arch" style={{ marginTop: ".6rem" }}>
            <thead><tr><th>Bill</th><th className="num">Amount</th><th>Cadence</th>
              <th>Archived</th><th><span className="sr-only">Actions</span></th></tr></thead>
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
          {/* the action is the button's own name, and only for those who
              may run it (a viewer has none) */}
          <EmptyState title={EMPTY_LINES.bills}
            action={mayEdit && !detect.isPending ? EMPTY_LINES.billsAction : undefined}
            onAction={() => detect.mutate()} />
        </div>
      )}
    </PastMonthCtx.Provider>
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
function BillTable(props: Omit<Parameters<typeof BillTableInner>[0], "past">) {
  const past = useContext(PastMonthCtx);
  return <BillTableInner {...props} past={!!past} />;
}

function BillTableInner({ past, groups, isEnv, income, edit, mayEdit, onEdit, onToggle,
                     onArchive, onPurge, onSaved, onHint, hintBusy,
                     togglePending, pendingDelete, prefill, cadences }: {
  past: boolean;
  groups: [string, Row[]][]; isEnv: boolean; income?: boolean;
  edit: string; mayEdit: boolean;
  onEdit: (payee: string | null) => void;
  onToggle: (payee: string, disabled: boolean) => void;
  onArchive: (payee: string) => void;
  onPurge: (payee: string) => void;
  onSaved: () => void;
  onHint: (payee: string, action: "dismiss" | "restore",
           target: "fit" | "health") => void;
  hintBusy: string | null;
  togglePending: boolean;
  pendingDelete: "archive" | "purge" | null;
  prefill: { pamt: string; pcad: string; pdue: string; pdays: string };
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
            <th>Status</th><th><span className="sr-only">Actions</span></th></tr>
        ) : (
          <tr><th>{income ? "Source" : "Bill"}</th>
            <th className="num">Amount</th>
            <th>{past ? "Due" : income ? "Next expected" : "Next due"}</th>
            <th>Cycle</th>
            <th>Status</th><th><span className="sr-only">Actions</span></th></tr>
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
  onToggle: (payee: string, disabled: boolean) => void;
  onArchive: (payee: string) => void;
  onPurge: (payee: string) => void;
  onSaved: () => void;
  onHint: (payee: string, action: "dismiss" | "restore",
           target: "fit" | "health") => void;
  hintBusy: boolean;
  togglePending: boolean;
  pendingDelete: "archive" | "purge" | null;
  prefill: { pamt: string; pcad: string; pdue: string; pdays: string };
  cadences: [string, string][];
}) {
  const { disabled } = b;
  // the row knows its own pool — no need to thread the group's shape down
  const isAnnualEnv = isEnv && b.period_months === 12;
  // the cycle position is the server's (cycle_frac, from the row's
  // cadence value); a past month has no countdown: its occurrences were paid, missed, or
  // not due, and the cycle bar has nothing to measure
  const past = useContext(PastMonthCtx);
  const settled = b.occurrences > 0 && b.paid >= b.occurrences;
  const missed = !!past && !settled && b.occurrences > 0;
  const overdue = b.due_tone === "late" || b.due_tone === "withdrawn";
  // a missed bill whose charge the bank feed withdrew: still unpaid, but a
  // warning to check rather than a missed payment — amber, not red
  const withdrawn = b.due_tone === "withdrawn";
  const withdrawnNote = withdrawn
    ? `charge withdrawn by the bank feed (${mmdd(b.withdrawn_on)})` : undefined;
  const soon = b.due_tone === "soon";
  const frac = b.cycle_frac ?? 0;
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
        {/* the server's sum of the bill and its fees (amount_with_fees) */}
        <td className="num c-amt">{money(b.amount_with_fees)}
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
          // — the bar's fill is the server's `period_frac` (the phone draws
          // the same one); a server without it draws no bar
          <td className="c-used">
            {b.period_frac != null && (
              <span className="envbar"
                    title={b.pool_text
                      ?? `${money(b.period_used)} of ${money(b.pool)}`}>
                <i className={(b.overflow ?? 0) > 0.005 ? "over" : ""}
                   style={{ width: `${100 * b.period_frac}%` }} />
              </span>
            )}
          </td>
        ) : (
          <td className="mut c-due" style={{ whiteSpace: "nowrap" }}>
            {past
              ? (b.next_unpaid ? mmdd(b.next_unpaid) : "\u00b7")
              : b.due_label ?? "\u00b7"}</td>
        )}
        {!isEnv && (
          // the bar says where you are in the cycle; the words beside it say
          // what the right edge IS — "in 12 days", "due today", "3 days late"
          // — in the bar's own colour, because the bar alone is not
          // intuitive. Amber when soon, red once overdue (the server's due_tone).
          <td className="c-cycle">
            <span className="cycle-cell">
              <span className="envbar"
                    title={b.cycle_frac == null ? undefined
                      : `${Math.round(frac * 100)}% through the cycle`}>
                <i className={withdrawn ? "soon" : overdue ? "over" : soon ? "soon" : ""}
                   style={{ width: `${frac * 100}%` }} />
              </span>
              {b.due_text ? (
                <span className={`cycle-words ${withdrawn ? "a" : overdue ? "r" : soon ? "a" : "g"}`}
                      title={withdrawnNote}>
                  {b.due_text}</span>
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
        {/* the status wraps rather than clip: a paid count with its last
            and next dates was cut off mid-word at desktop width */}
        <td className="c-state">
          {disabled ? <span className="pill m">disabled</span>
            : past && !isEnv && !settled ? (
              // the month on view is over: paid, missed, or never due
              missed
                ? (b.withdrawn_on
                    ? <span className="pill a" title={`charge withdrawn by the bank feed (${mmdd(b.withdrawn_on)})`}>withdrawn</span>
                    : <span className="pill r">{b.income ? "not received" : "missed"}</span>)
                : <span className="sub">not due</span>
            ) : isEnv ? (
              (b.overflow ?? 0) > 0.005
                ? <span className="pill r">over +{money(b.overflow!)}</span>
                // what is left of the pool is the server's (period_left)
                : b.period_left == null ? null
                : b.period_left <= b.amount * 0.001
                  // the pool is spent: "used up", not "full" (which read
                  // as a pool still holding its money)
                  ? <span className="pill a">used up{isAnnualEnv ? " this year" : ""}</span>
                  : <span className="sub">
                      {money(b.period_left)} left
                      {isAnnualEnv ? " this year" : ""}</span>
            ) : b.income
              ? (settled
                  // the server's words: "received 05/15", "2 of 2 received"
                  ? <span className="sub">{b.paid_text}</span>
                  : b.due_text
                    // the Cycle column already says this on desktop; the
                    // copy here is for phone width, where Cycle is hidden
                    ? <>{b.paid_text && <span className="sub">{b.paid_text} </span>}
                        <span className="sub cycle-dup">{b.due_text}</span></>
                    // dateless but unsettled must still say so \u2014 a blank
                    // status cell is reserved for "nothing to decide"
                    : <span className="pill m"
                        title="expected, but no date on file">awaiting</span>)
            : settled
              // the server's words: "paid 05/03 · next 06/03" when the
              // date beside it is already next month's, "2 of 2 paid"
              ? <span className="sub">{b.paid_text}</span>
              : withdrawn
                ? <span className="pill a" title={withdrawnNote}>withdrawn</span>
              : overdue
                ? <span className="pill r">overdue</span>
                : b.due_text
                  // a bill due twice that is half paid says "1 of 2 paid"
                  ? <>{b.paid_text && <span className="sub">{b.paid_text} </span>}
                      <span className="sub cycle-dup">{b.due_text}</span></>
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
                      aria-label="Dismiss this flag"
                      style={FLAGBTN} disabled={hintBusy}
                      onClick={() => onHint(b.payee, "dismiss", "health")}>✕</button>
              )}
            </>
          )}
          {/* a cycle whose charge the bank feed withdrew: a warning, not a
              problem to decide — no dismiss, no attention count, and not
              repeated when the status column already says "withdrawn" */}
          {!disabled && b.health === "withdrawn" && !withdrawn && (
            <>{" "}{hpill(b.health, b.last_seen, b.health_withdrawn_on)}</>
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
                      aria-label="Dismiss this hint"
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
                 onToggle={() => onToggle(b.payee, !b.disabled)}
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
// arbitrary FREQ:interval (every X days / weeks / months / years); the engine already
// schedules + budgets any interval (budget._occurrences_in_month honors it).
/** The payee an add was refused over because an archived bill holds the
 *  name, or null for any other failure. */
function archivedClash(e: unknown): string | null {
  const m = /^409:\s*([\s\S]*)$/.exec(e instanceof Error ? e.message : "");
  try {
    const j = JSON.parse(m?.[1] ?? "");
    return j?.archived === true ? String(j.payee || "") : null;
  } catch { return null; }
}

function AddBillForm({ onCancel, onSaved }:
                     { onCancel: () => void;
                       onSaved: (restored: boolean) => void }) {
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
  // the add hit an archived bill of the same name: offer to bring it back
  // with the values entered here
  const [archived, setArchived] = useState(false);
  const cadence = mode === "CUSTOM" ? customCadence(customUnit, customN) : mode;
  const isEnv = cadence.startsWith("ENVELOPE");
  const save = useMutation({
    mutationFn: (restore: boolean) => api.billsSave({
      payee: payee.trim(), amount, cadence,
      next_due: nextDue, income, merchant: payee.trim(),
      ...(income ? {} : { show_today: pinned }),
      // an envelope with a category counts that category's spend toward the
      // pool — the natural read of picking one on a pool that has no
      // merchant history yet
      ...(isEnv && category.trim()
        ? { category: category.trim(), match_category: true } : {}),
      ...(restore ? { restore: true } : {}),
    }),
    onMutate: () => { setErr(null); setArchived(false); },
    onSuccess: (r) => onSaved(!!r?.restored),
    onError: (e) => {
      setErr(errText(e));
      setArchived(archivedClash(e) !== null);
    },
  });
  return (
    <div className="card" style={{ marginBottom: "1rem" }}>
      <form style={{ display: "flex", gap: ".6rem", alignItems: "center",
                     flexWrap: "wrap" }}
            onSubmit={(e) => {
              e.preventDefault();
              if (payee.trim() && amount) save.mutate(false);
            }}>
        <input aria-label="Merchant" type="text" required list="known-merchants"
               placeholder="merchant — pick a known one or type a new name"
               title="pick an existing merchant so this bill matches its past + future transactions"
               value={payee} style={{ width: "18rem" }}
               onChange={(e) => setPayee(e.target.value)} />
        <datalist id="known-merchants">
          {(merchants.data?.merchants ?? []).map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
        <label className="mut">$ <input aria-label="Amount" type="number" step="0.01" min="0"
               required placeholder="0.00" value={amount} style={{ width: "7rem" }}
               onChange={(e) => setAmount(e.target.value)} /></label>
        <select aria-label="How often" value={mode} onChange={(e) => setMode(e.target.value)}>
          {SIMPLE_CADENCES.map(([v, label]) => (
            <option key={v} value={v}>{label}</option>
          ))}
          <option value="CUSTOM">Custom interval…</option>
        </select>
        {mode === "CUSTOM" && (
          <label className="mut">every <input type="number" min="1" max={customMax(customUnit)}
                 value={customN} style={{ width: "3.5rem" }}
                 onChange={(e) => setCustomN(e.target.value)} />
            <select value={customUnit}
                    onChange={(e) => setCustomUnit(e.target.value)}>
              {CUSTOM_UNITS.map(([v, label]) => (
                <option key={v} value={v}>{label}</option>
              ))}
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
                <option key={c} value={c} label={catLabel(c)} />
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
      {err && <div className="neg" style={{ fontSize: 13, marginTop: ".4rem" }}>
        {err}
        {archived && (
          <button type="button" className="pri" style={{ marginLeft: ".5rem" }}
                  disabled={save.isPending}
                  onClick={() => save.mutate(true)}>Restore</button>
        )}
      </div>}
    </div>
  );
}

function EditRow({ d, cadences, cols, prefill, togglePending, pendingDelete,
                   onCancel, onSaved, onToggle, onArchive, onPurge }: {
  d: Bill; cadences: [string, string][]; cols: number;
  prefill: { pamt: string; pcad: string; pdue: string; pdays: string };
  togglePending: boolean; pendingDelete: "archive" | "purge" | null;
  onCancel: () => void; onSaved: () => void;
  onToggle: () => void;
  onArchive: () => void; onPurge: () => void;
}) {
  const [payee, setPayee] = useState(d.payee);
  const [amount, setAmount] = useState(prefill.pamt || d.amount.toFixed(2));
  // the days the picker's several-days option stands for: a ledger-fit
  // prefill speaks for itself, else the bill's own rule
  const days = prefill.pcad
    ? prefill.pdays.split(",").map(Number).filter(Number.isInteger)
    : d.by_month_day;
  const [cadence, setCadence] = useState(
    cadencePick(prefill.pcad || d.cadence_value, days));
  const [nextDue, setNextDue] = useState(prefill.pdue || d.due_on || "");
  // the stored due date when the form opened: a save sends next_due only
  // when the field differs from it, so a name or amount edit leaves the
  // date to the server, which may have rolled it on since this was read
  const [seededDue] = useState(d.due_on ?? "");
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
      payee, amount, income: d.income,
      ...cadenceFields(cadence, d.by_month_day, days, nextDue),
      ...(nextDue && nextDue !== seededDue ? { next_due: nextDue } : {}),
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
          <input aria-label="Merchant" type="text" required value={payee} style={{ width: "16rem" }}
                 title="renaming changes ledger matching (first significant word of the name)"
                 onChange={(e) => setPayee(e.target.value)} />
          <label className="mut">$ <input aria-label="Amount" type="number" step="0.01" min="0"
                 value={amount} style={{ width: "6.5rem" }}
                 onChange={(e) => setAmount(e.target.value)} /></label>
          <select aria-label="How often"
                  value={cadence} onChange={(e) => setCadence(e.target.value)}>
            {/* the bill's saved interval (and a ledger-fit prefill) stay
                pickable even when the server's standard list lacks them */}
            {withSeveralDays(cadenceChoices(cadences, [d.cadence_value, prefill.pcad]),
                             onSeveralDays(days) ? days : d.by_month_day)
              .map(([v, label]) => (
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
                  <option key={c} value={c} label={catLabel(c)} />
                ))}
              </datalist>
              <label className="mut"
                     title="spend in this category counts toward the pool even when the merchant doesn't match — for pools fed by many merchants (a vet, a groomer, a pet store)">
                <input type="checkbox" checked={matchCat && !!category.trim()}
                       disabled={!category.trim()}
                       onChange={(e) => setMatchCat(e.target.checked)} />
                {" "}count all {category.trim() ? catLabel(category.trim()) : "category"} spending
                {/* the envelope's own charges carry another category: say
                    so, so the box is not ticked on a guess */}
                {d.charges_category && category.trim()
                  && catLabel(d.charges_category) !== catLabel(category.trim()) && (
                  <span className="sub"> — its charges this month are{" "}
                    {catLabel(d.charges_category)}</span>)}
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
                        aria-label={`Remove ${m}`}
                        onClick={() => setMerchants(merchants.filter((x) => x !== m))}>
                  ×</button>
              </span>
            ))}
            {merchants.length < 20 && (
              <>
                <input aria-label="Add a merchant"
                       type="text" list="bill-merchants" value={newMerchant}
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
                  <input aria-label={`Fee ${i + 1}: words on the charge`}
                         value={f.tokens} placeholder="words on the charge"
                         maxLength={FEE_WORDS_MAX}
                         style={{ width: "12rem" }}
                         onChange={(e) => setFees(fees.map((x, j) =>
                           j === i ? { ...x, tokens: e.target.value } : x))} />
                  <input aria-label={`Fee ${i + 1}: amount`}
                         value={f.amount} placeholder="$" inputMode="decimal"
                         style={{ width: "4.5rem" }}
                         onChange={(e) => setFees(fees.map((x, j) =>
                           j === i ? { ...x, amount: e.target.value } : x))} />
                  <span>within</span>
                  <input aria-label={`Fee ${i + 1}: within how many days`}
                         value={f.window_days} inputMode="numeric"
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

type CalDay = CalendarData["calendar"][number];

function UpcomingCalendar() {
  const q = useQuery({ queryKey: ["calendar"], queryFn: () => api.calendar(35) });
  // a fetch that never resolves would render as nothing at all — the
  // section owns its own query, so it owns saying so too (mobile parity)
  if (q.isPending || q.isLoadingError) {
    return (
      <div className="card">
        <h2>Next five weeks</h2>
        {q.isPending ? <p className="mut">loading…</p>
          : <FailedLine q={q} what="the calendar" />}
      </div>
    );
  }
  // browser-local today. The server's start is the HOUSEHOLD's day, not
  // UTC-today — UTC would highlight tomorrow for a US evening user — and
  // the device clock still wins for a travelling user. Events before
  // local-today stay dimmed either way.
  const todayIso = isoDay(new Date());
  const twoWeeks = new Date();
  twoWeeks.setDate(twoWeeks.getDate() + 13);
  return <CalendarGrid title="Next five weeks" startIso={q.data.start}
                       weeks={5} days={q.data.calendar} todayIso={todayIso}
                       agendaTitle="The next two weeks"
                       agendaFrom={todayIso} agendaTo={isoDay(twoWeeks)} />;
}

/** A closed month's calendar: the bills it paid on the days they were
 *  paid, the paychecks it received, and the bills it left unpaid on their
 *  due days — from the Bills reply for that month, nothing added here. */
function MonthCalendar({ ym, days }: { ym: string; days: CalDay[] }) {
  const first = new Date(ym + "-01T00:00:00");
  const last = new Date(first.getFullYear(), first.getMonth() + 1, 0);
  const lead = (first.getDay() + 6) % 7;
  const weeks = Math.ceil((lead + last.getDate()) / 7);
  return <CalendarGrid title={ymLabel(ym)} startIso={ym + "-01"}
                       weeks={weeks} days={days} todayIso={null}
                       inMonth={ym} agendaTitle={`${ymLabel(ym)}, day by day`}
                       agendaFrom={ym + "-01"} agendaTo={isoDay(last)} />;
}

const isoDay = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function CalendarGrid({ title, startIso, weeks, days, todayIso, inMonth,
                        agendaTitle, agendaFrom, agendaTo }: {
  title: string; startIso: string; weeks: number; days: CalDay[];
  /** null: a closed month — no day is "today" and none is dimmed */
  todayIso: string | null;
  /** a month's grid: the padding days of its neighbours stay empty */
  inMonth?: string;
  agendaTitle: string; agendaFrom: string; agendaTo: string;
}) {
  const byDate = new Map(days.map((d) => [d.date, d]));
  // whole ISO weeks starting Monday of the start's week
  const start = new Date(startIso + "T00:00:00");
  const monday = new Date(start);
  monday.setDate(start.getDate() - ((start.getDay() + 6) % 7));
  const cells: Date[] = Array.from({ length: weeks * 7 }, (_, i) => {
    const d = new Date(monday);
    d.setDate(monday.getDate() + i);
    return d;
  });

  // The grid IS the agenda: each day carries its figures and labels, and
  // the full list sits in the cell's tooltip. Per-date cards below it
  // would repeat the same events in a second shape and push the Archived
  // table off the bottom of the page — on a wide screen. At phone width a
  // seventh of the screen holds four letters of a payee, which named
  // nothing, so there the cells carry only the day's figures and a short
  // list names what is due (index.css shows one or the other).
  const soon = cells.map(isoDay).filter((k) => k >= agendaFrom
    && k <= agendaTo && (byDate.get(k)?.events.length ?? 0) > 0);
  const WD = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
  return (
    <div className="card">
      <h2>{title}</h2>
      <div className="cal-grid">
        {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => (
          <div key={d} className="cal-head">{d}</div>
        ))}
        {cells.map((d) => {
          const key = isoDay(d);
          const outside = !!inMonth && key.slice(0, 7) !== inMonth;
          const day = outside ? undefined : byDate.get(key);
          const past = todayIso !== null && key < todayIso;
          const show = !!day && !past;
          return (
            <div key={key}
                 className={"cal-cell" + (key === todayIso ? " today" : "") +
                            (past || outside ? " past" : "")}
                 title={day?.events.map((e) =>
                   `${e.label} ${e.amount > 0 ? "+" : "−"}${money(Math.abs(e.amount))}`).join("\n")}>
              <span className="cal-day">{d.getDate()}</span>
              {/* a day's in and out, each with its sign, never netted: a
                  paycheck and a bill on one day are two facts, and a net
                  hid the bill behind the paycheck. Whole dollars — a cell
                  is a seventh of the page (the hover keeps the cents) */}
              {show && day.came_in > 0 && (
                <span className="cal-amt in">+{money$(day.came_in)}</span>)}
              {show && day.went_out > 0 && (
                <span className="cal-amt out">−{money$(day.went_out)}</span>)}
              {/* the name and what its figure is on lines of their own:
                  joined, a narrow cell cut "full balance" mid-word */}
              {show && day.events.slice(0, 2).map((e, i) => (
                <span key={i} className="cal-label">{e.name}
                  {e.note && <span className="cal-note">{e.note}</span>}</span>
              ))}
              {show && day.events.length > 2 && (
                <span className="cal-label mut">+{day.events.length - 2}<span className="hide-m"> more</span></span>
              )}
            </div>
          );
        })}
      </div>
      {soon.length > 0 && (
        <div className="cal-agenda">
          <div className="sub" style={{ margin: ".8rem 0 .3rem" }}>
            {agendaTitle}</div>
          {soon.map((k) => {
            const day = byDate.get(k)!;
            const d = new Date(k + "T00:00:00");
            return (
              <div key={k} className="cal-ag-day">
                <span className="cal-ag-date">
                  {WD[d.getDay()]} {d.getDate()}</span>
                <div className="cal-ag-evs">
                  {day.events.map((e, i) => (
                    <div key={i} className="cal-ag-ev">
                      <span className="cal-ag-name">{e.name}
                        {e.note && <span className="cal-note">{e.note}</span>}</span>
                      {/* a bill on a card moves no checking that day:
                          greyed, as the Today timeline greys it */}
                      <span className={"num " + (e.amount > 0 ? "pos"
                                                 : e.kind === "on_card" ? "mut" : "")}>
                        {e.amount > 0 ? "+" : ""}{money(Math.abs(e.amount))}</span>
                    </div>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// One line of the triage queue: severity dot, the story the ledger tells,
// and the ONE action the server's evidence supports. The story is the
// server's (attention_text, api_bills.attention_words) — the client never
// invents an action, a date or a number.
function AttentionLine({ b, mayEdit, busy, onJump, onAccept, onDisable,
                         onDismiss }: {
  b: Bill; mayEdit: boolean; busy: boolean; onJump: () => void;
  onAccept: (amt: number) => void; onDisable: () => void;
  onDismiss: () => void;
}) {
  const s = b.suggestion;
  const story = b.attention_text ?? "";
  return (
    <div style={{ display: "flex", gap: ".4rem .7rem", alignItems: "center",
                  padding: ".45rem .7rem",
                  // at a phone width the buttons drop under the sentence
                  // instead of squeezing it into a column of two words
                  flexWrap: "wrap", justifyContent: "flex-end",
                  borderBottom: "1px solid var(--line)", fontSize: 13 }}>
      <span style={{ width: 8, height: 8, borderRadius: "50%", flex: "none",
                     background: b.health === "stale"
                       ? "var(--bad, #f85149)" : "var(--warn, #d29922)" }} />
      <button className="linklike" onClick={onJump}
              style={{ ...LINKLIKE, flex: "1 1 13rem", textAlign: "left",
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
