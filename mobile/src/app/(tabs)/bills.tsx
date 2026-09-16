// Bills — the recurring schedule, read-only. Mirrors the web page's
// anatomy: headline totals, attention count, proposals nudge, then the
// active schedule with per-bill state (envelopes show their pool).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Pressable, ScrollView, StyleSheet, Switch, Text,
         TextInput, View } from "react-native";
import PullRefresh from "../../components/pull-refresh";
import Sheet from "../../components/sheet";

import CalendarSection from "../../components/calendar-section";
import StaleBanner from "../../components/stale-banner";
import { Card, H, HelpLink, KV, Meter, Pill } from "../../components/ui";
import { errText, Bill, BillsData, Proposal } from "../../lib/api";
import { patchDismissedHint, patchList, removeFromList } from "../../lib/cache";
import { useRouter, useScrollToTop } from "expo-router";
import { useMemo, useRef, useState } from "react";

import { useSession } from "../../lib/session";
import { useViewer } from "../../lib/viewer";
import { C, mmddyy, money } from "../../lib/theme";
import { catLabel, filterCategories } from "../../lib/pure";

// the web row's status column, ported whole: a quiet row IS the ✓ —
// only a bill wanting a decision says anything
const DAY = 86_400_000;
function daysUntil(iso: string): number {
  const today = new Date(); today.setHours(0, 0, 0, 0);
  return Math.round(
    (new Date(iso + "T00:00:00").getTime() - today.getTime()) / DAY);
}
const dueText = (n: number, income?: boolean) =>
  n === 0 ? (income ? "today" : "due today")
  : n === 1 ? (income ? "tomorrow" : "due tomorrow")
  : n === -1 ? "1 day late"
  : n < 0 ? `${-n} days late`
  : `in ${n} days`;
// the cycle bar turns amber this close to the due date — same rule and
// colours as the web's Cycle column, where the words beside the bar carry
// the same colour as the fill
const SOON_DAYS = 3;
// list rows carry the human cadence label; recover cycle length from it
function cycleDays(label: string): number {
  const units: Record<string, number> = {
    da: 1, week: 7, month: 30.44, year: 365.25 };
  const m = /^every (\d+) (da|week|month|year)/.exec(label);
  if (m) return units[m[2]] * parseInt(m[1], 10);
  return { daily: 1, weekly: 7, monthly: 30.44,
           yearly: 365.25 }[label] ?? 0;
}
// only these are problems worth a pill — off-ledger is a way of life
// for cash/manual bills, and ok is silence
const HEALTH_PILL: Record<string, "warn" | "bad"> = {
  drifting: "warn", stale: "bad", mismatch: "warn", misdated: "warn",
};

function BillRow({ b, onPress, onHint }: {
  b: Bill; onPress?: () => void;
  // dismiss/restore a flag from the row (web's ✕ / ↺) — absent = viewer
  onHint?: (payee: string, action: "dismiss" | "restore",
            target: "fit" | "health") => void;
}) {
  const envelope = b.used !== null;
  const isAnnualEnv = envelope && b.period_months === 12;
  const daysTo = !envelope && b.due_on ? daysUntil(b.due_on) : null;
  const settled = b.occurrences > 0 && b.paid >= b.occurrences;
  const overdue = !settled && daysTo !== null && daysTo < 0;
  const soon = !settled && daysTo !== null && daysTo >= 0 && daysTo <= SOON_DAYS;
  const cyc = cycleDays(b.cadence);
  const cycleFrac = daysTo === null || cyc <= 0 ? null
    : Math.min(1, Math.max(0, 1 - daysTo / cyc));

  let status: React.ReactNode;
  if (envelope) {
    const left = b.period_left ?? b.amount - (b.used ?? 0);
    status = (b.overflow ?? 0) > 0.005
      ? <Pill text={`over +${money(b.overflow!, false)}`} tone="bad" />
      : left <= b.amount * 0.001
        ? <Pill text="full" tone="warn" />
        : <Text style={s.mut}>
            {money(left, false)} left{isAnnualEnv ? " this year" : ""}
          </Text>;
  } else if (b.income) {
    status = settled
      ? <Text style={{ color: C.good, fontSize: 12 }}>
          received ✓{b.paid > 1 ? ` ×${b.paid}` : ""}
        </Text>
      : daysTo !== null
        ? <Text style={[s.mut, { color: soon ? C.warn : C.good }]}>{dueText(daysTo, true)}</Text>
        : <Pill text="awaiting" />;
  } else {
    status = settled
      ? <Text style={s.mut}>paid ✓</Text>
      : overdue
        ? <Pill text="overdue" tone="bad" />
        : daysTo !== null
          ? <Text style={[s.mut, { color: soon ? C.warn : C.good }]}>{dueText(daysTo)}</Text>
          : <Pill text="awaiting" />;
  }
  return (
    <Pressable style={({ pressed }) => [s.bill,
                 pressed && onPress ? { backgroundColor: C.hover } : null]}
               onPress={onPress}>
      <View style={s.billTop}>
        <Text style={s.payee} numberOfLines={1}>{b.payee}</Text>
        <Text style={[s.amount, b.income && { color: C.good }]}>
          {b.income ? "+" : ""}{money(b.amount + (b.fee_total || 0))}
          {b.fee_total
            ? <Text style={{ color: C.mut, fontSize: 11 }}>
                {"  incl. "}{money(b.fee_total)}{" fee"}</Text> : null}
        </Text>
      </View>
      <View style={s.billSub}>
        <Text style={s.mut}>
          {b.cadence}
          {b.due_on ? ` · due ${mmddyy(b.due_on)}`
            : b.next_unpaid ? ` · next ${mmddyy(b.next_unpaid)}` : ""}
        </Text>
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 6 }}>
          {status}
          {b.disabled && <Pill text="disabled" />}
          {!b.disabled && b.health && !b.health_dismissed
            && HEALTH_PILL[b.health] ? (
            <>
              <Pill text={b.health === "mismatch" && b.last_seen
                ? `mismatch · ledger ${money(b.last_seen.amount)} on ${mmddyy(b.last_seen.date)}`
                : b.health} tone={HEALTH_PILL[b.health]} />
              {/* dismiss — it returns only if the problem changes */}
              {onHint && (
                <Text style={{ color: C.mut, fontSize: 12 }}
                      onPress={() => onHint(b.payee, "dismiss", "health")}>
                  ✕
                </Text>
              )}
            </>
          ) : null}
          {/* the way back from a dismissed health flag */}
          {!b.disabled && b.health && b.health_dismissed
            && HEALTH_PILL[b.health] && onHint ? (
            <Text style={{ color: C.mut, fontSize: 12 }}
                  onPress={() => onHint(b.payee, "restore", "health")}>
              ↺
            </Text>
          ) : null}
          {/* the ledger disagrees with this bill — the 💡 opens the
              history, where "edit with these values" applies the fit */}
          {!b.disabled && b.fit && !b.hint_dismissed
            && (["mismatch", "stale", "misdated"].includes(b.health ?? "")
                || b.fit_diverges)
            ? (
            <>
              <Text style={{ fontSize: 12 }}>💡</Text>
              {onHint && (
                <Text style={{ color: C.mut, fontSize: 12 }}
                      onPress={() => onHint(b.payee, "dismiss", "fit")}>
                  ✕
                </Text>
              )}
            </>
          ) : null}
        </View>
      </View>
      {envelope && b.pool ? (
        <Meter frac={(b.period_used ?? 0) / (b.pool || 1)}
               tone={b.overflow ? "bad" : undefined} />
      ) : cycleFrac !== null && !settled ? (
        <Meter frac={cycleFrac} tone={overdue ? "bad" : soon ? "warn" : undefined} />
      ) : null}
    </Pressable>
  );
}

// the web page's cadence groups, in its rank order — every interval gets
// its own heading instead of an "Other" catch-all
const GROUP_ORDER = ["Weekly", "Every 14 days", "Every 2 weeks", "Monthly",
                     "Monthly envelope", "Annual envelope",
                     "Every 8 weeks", "Every 2 months", "Every 3 months",
                     "Every 6 months", "Yearly", "Every 2 years",
                     "One-time / other"];
function cadenceGroup(b: Bill): string {
  if (b.used !== null)
    return b.period_months === 12 ? "Annual envelope" : "Monthly envelope";
  const c = b.cadence.toLowerCase();
  const named: Record<string, string> = {
    weekly: "Weekly", "every 14 days": "Every 14 days",
    "every 2 weeks": "Every 2 weeks", monthly: "Monthly",
    "every 8 weeks": "Every 8 weeks", "every 2 months": "Every 2 months",
    "every 3 months": "Every 3 months", quarterly: "Every 3 months",
    "every 6 months": "Every 6 months", yearly: "Yearly",
    "every 2 years": "Every 2 years",
  };
  return named[c] ?? "One-time / other";
}

export default function Bills() {
  const { client } = useSession();
  // re-tapping the active tab scrolls back to the top
  const topRef = useRef<ScrollView>(null);
  useScrollToTop(topRef);
  const router = useRouter();
  const viewer = useViewer();
  const qc = useQueryClient();
  const query = useQuery({
    queryKey: ["bills"],
    queryFn: () => client!.bills(),
    enabled: !!client,
  });
  // every write says what it did — silent success reads as a dead tap
  const [notice, setNotice] = useState<string | null>(null);
  // one reversible triage action at a time (the web banner's Undo)
  const [undoAct, setUndoAct] = useState<{ label: string;
                                           act: () => void } | null>(null);
  // every schedule write refreshes the 5-week calendar below AND the
  // Today tab, like the web's refetchAll — an approved bill shows up in
  // the grid, and adding one changes Today's numbers
  const refetchSchedule = () => {
    qc.invalidateQueries({ queryKey: ["bills"] });
    qc.invalidateQueries({ queryKey: ["calendar"] });
    qc.invalidateQueries({ queryKey: ["today"] });
  };
  // a decided proposal leaves the list NOW — the server has already
  // committed it; waiting for /api/bills to re-prove that left the row
  // sitting there. Approving writes a bill (the calendar, Today and the
  // budgets prefill move); rejecting writes none, so only the list, the
  // prefill — which reads PENDING income proposals — and the two
  // pending-proposal counts (Today's alert, the wizard's line) change.
  const dropProposal = (pid: string) =>
    removeFromList<BillsData, Proposal>(qc, ["bills"], "proposals",
      (p) => p.id === pid);
  const act = useMutation({
    mutationFn: ({ pid, action }:
        { pid: string; action: "approve" | "reject" }) =>
      client!.billsProposal(pid, action),
    onSuccess: (r, v) => {
      setNotice(v.action === "approve"
        ? "Approved — the bill is live." : "Proposal rejected.");
      dropProposal(v.pid);
      if (v.action === "approve") {
        refetchSchedule();
        qc.invalidateQueries({ queryKey: ["payee-history", r.payee] });
      } else {
        qc.invalidateQueries({ queryKey: ["bills"] });
        qc.invalidateQueries({ queryKey: ["today"] });
        qc.invalidateQueries({ queryKey: ["onboarding"] });
      }
      qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    },
  });
  const approveAll = useMutation({
    mutationFn: () => client!.billsApproveAll(),
    onSuccess: (r) => {
      setNotice(r.failed.length
        ? `Approved ${r.approved}; ${r.failed.length} stayed pending.`
        : `Approved all ${r.approved} proposal${r.approved === 1 ? "" : "s"}.`);
      // only what stayed pending is still a proposal
      qc.setQueryData<BillsData>(["bills"], (old) => old && {
        ...old,
        proposals: old.proposals.filter(
          (p) => r.failed.some((f) => f.pid === p.id)),
      });
      refetchSchedule();
      qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    },
    onError: (e) => Alert.alert("Approve all failed", errText(e)),
  });
  const detect = useMutation({
    mutationFn: () => client!.billsDetect(),
    onSuccess: refetchSchedule,
    onError: (e) => Alert.alert("Scan failed", errText(e)),
  });
  const restore = useMutation({
    mutationFn: (payee: string) => client!.billsRestore(payee),
    onSuccess: (_r, payee) => {
      setNotice(`Restored ${payee}.`);
      removeFromList<BillsData, BillsData["archived"][number]>(
        qc, ["bills"], "archived", (b) => b.payee === payee);
      refetchSchedule();
    },
    onError: (e) => Alert.alert("Restore failed", errText(e)),
  });
  // a mutation, so a double tap while the purge runs is ignored and a
  // failure surfaces as its own Alert
  const delForever = useMutation({
    mutationFn: (payee: string) => client!.billsDelete(payee, "purge"),
    onSuccess: (_r, payee) => {
      removeFromList<BillsData, BillsData["archived"][number]>(
        qc, ["bills"], "archived", (b) => b.payee === payee);
      qc.invalidateQueries({ queryKey: ["bills"] });
    },
    onError: (e) => Alert.alert("Couldn't delete", errText(e)),
  });
  // A flag dismissal is certain, so the pill goes (or comes back) in the
  // same frame as the tap and the attention count follows; rolled back
  // if the server refuses. It moves no money — only the list itself and
  // the settings (which carry the dismissed hints) are refetched.
  const hint = useMutation({
    mutationFn: ({ payee, action, target }: {
      payee: string; action: "dismiss" | "restore";
      target: "fit" | "health";
    }) => client!.billsHint(payee, action, target),
    onMutate: async ({ payee, action, target }) => {
      await qc.cancelQueries({ queryKey: ["bills"] });
      const prev = qc.getQueryData<BillsData>(["bills"]);
      const dismissed = action === "dismiss";
      qc.setQueryData<BillsData>(["bills"], (old) => old && {
        ...old,
        bills: old.bills.map((b) => b.payee !== payee ? b
          : target === "health" ? { ...b, health_dismissed: dismissed }
          : { ...b, hint_dismissed: dismissed }),
        totals: old.totals && target === "health" ? {
          ...old.totals,
          attention: Math.max(0, old.totals.attention + (dismissed ? -1 : 1)),
        } : old.totals,
      });
      return { prev };
    },
    onSuccess: (_r, v) => {
      // the settings tweak count on Budget only tracks "fit" dismissals
      // (health lives under a separate, unread settings key server-side)
      if (v.target === "fit")
        patchDismissedHint(qc, v.payee, v.action === "dismiss");
      qc.invalidateQueries({ queryKey: ["bills"] });
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["bills"], ctx.prev);
      Alert.alert("Couldn't update the flag", errText(e));
    },
  });
  const onHint = viewer ? undefined
    : (payee: string, action: "dismiss" | "restore",
       target: "fit" | "health") => {
      // ✕ becomes ↺ the moment it is tapped — a second tap on the same
      // row mid-flight would be a restore racing the dismiss
      if (hint.isPending && hint.variables?.payee === payee) return;
      hint.mutate({ payee, action, target });
    };
  const [adding, setAdding] = useState(false);
  const [showArch, setShowArch] = useState(false);
  // the draft's own fields live in AddBillFields (keyed on `adding`), so
  // a keystroke re-renders only that component, not every BillRow, the
  // cadence-group passes below, the envelope IIFE, the archived list and
  // CalendarSection
  // triage queue actions — Accept is just an edit of the bill's amount
  // (Undo saves the old one back); Disable flips the budget flag
  const accept = useMutation({
    mutationFn: ({ b, amt }: { b: Bill; amt: number }) =>
      client!.billsSave({ payee: b.payee, amount: amt,
                          // omitted when unknown: the server keeps the
                          // bill's own cadence rather than defaulting
                          ...(b.cadence_value ? { cadence: b.cadence_value }
                                              : {}),
                          next_due: b.due_on ?? undefined,
                          income: b.income }),
    onSuccess: (_r, v) => {
      setUndoAct({
        label: `${v.b.payee} updated to ${money(v.amt)}.`,
        act: () => accept.mutate({ b: v.b, amt: v.b.amount }) });
      refetchSchedule();
    },
    onError: (e) => Alert.alert("Couldn't update", errText(e)),
  });
  const disable = useMutation({
    mutationFn: (v: { payee: string; disabled: boolean }) =>
      client!.billsToggle(v.payee, v.disabled),
    onSuccess: (_r, v) => {
      // Undo arms only once the server confirms: armed on tap, a failed
      // disable (flaky network) leaves a false "Disabled X." banner whose
      // Undo later fires blind against whatever the bill's real state is
      if (v.disabled) setUndoAct({ label: `Disabled ${v.payee}.`,
                                   act: () => disable.mutate(
                                     { payee: v.payee, disabled: false }) });
      refetchSchedule();
    },
    // no setUndoAct(null) here: with arming moved to onSuccess a failed
    // disable never armed anything, and clearing would wipe an unrelated
    // "Accepted $X" Undo still on screen (the web card keeps it too)
    onError: (e) => Alert.alert("Couldn't disable", errText(e)),
  });

  const add = useMutation({
    mutationFn: (f: AddBillForm) => client!.billsSave({
      payee: f.payee.trim(), amount: f.amount, cadence: f.cadence,
      next_due: f.due || undefined, income: f.income,
      ...(f.income ? {} : { show_today: f.pinned }),
      // an envelope with a category counts that category's spend toward
      // the pool — the natural read of picking one on a fresh pool
      ...(f.isEnv && f.category.trim()
        ? { category: f.category.trim(), match_category: true } : {}),
    }),
    onSuccess: (_r, f) => {
      setNotice(`Added ${f.payee.trim()}.`);
      setAdding(false);
      refetchSchedule();
    },
  });
  const d = query.data;
  // one pass over the schedule instead of GROUP_ORDER.length separate
  // filters (each running cadenceGroup's regex+lookup per bill again)
  const { bills, income, billsByGroup } = useMemo(() => {
    const billsList: Bill[] = [];
    const incomeList: Bill[] = [];
    const byGroup = new Map<string, Bill[]>();
    for (const b of d?.bills ?? []) {
      if (b.income) { incomeList.push(b); continue; }
      billsList.push(b);
      const key = cadenceGroup(b);
      const arr = byGroup.get(key);
      if (arr) arr.push(b); else byGroup.set(key, [b]);
    }
    return { bills: billsList, income: incomeList, billsByGroup: byGroup };
  }, [d]);

  return (
    <ScrollView ref={topRef} style={s.wrap}
      contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => {
                                        // the calendar section below runs
                                        // its own query — a pull that only
                                        // refetched bills left a failed
                                        // calendar fetch stuck forever
                                        return Promise.all([
                                          query.refetch(),
                                          qc.invalidateQueries(
                                            { queryKey: ["calendar"] })]);
                                      }} />}>
      <StaleBanner query={query} />
      {!viewer && (
      <View style={s.toolRow}>
        <Pressable style={s.tool} onPress={() => setAdding(true)}>
          <Text style={s.toolText}>＋ Add a bill</Text>
        </Pressable>
        <Pressable style={[s.tool, detect.isPending && { opacity: 0.5 }]}
                   disabled={detect.isPending}
                   onPress={() => detect.mutate()}>
          <Text style={s.toolText}>
            {detect.isPending ? "Scanning…" : "⌕ Find bills & income"}
          </Text>
        </Pressable>
      </View>
      )}
      {detect.data && (() => {
        const nInc = detect.data.proposed_income ?? 0;
        const nBill = Math.max(0, (detect.data.proposed_add ?? 0) - nInc);
        const parts: string[] = [];
        if (nInc) parts.push(`${nInc} income series`);
        if (nBill) parts.push(`${nBill} bill${nBill === 1 ? "" : "s"}`);
        if (!parts.length && detect.data.proposed_add)
          parts.push(`${detect.data.proposed_add} proposal${
            detect.data.proposed_add === 1 ? "" : "s"}`);
        return (
          <Text style={[s.mut, { textAlign: "center" }]}>
            Scan complete:{" "}
            {parts.length ? parts.join(" + ") : "no new proposals"}
            {detect.data.drift_applied
              ? `, ${detect.data.drift_applied} amount${
                  detect.data.drift_applied === 1 ? "" : "s"} updated`
              : ""}.
          </Text>
        );
      })()}
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {d?.totals && (
        <Card>
          <KV k="Monthly bill load" v={money(d.totals.bills_monthly, false)} />
          <KV k="Monthly income" v={money(d.totals.income_monthly, false)}
              tone="good" />
          {/* the difference is what the variable budget and savings
              actually have to work with (matches the web hero) */}
          <KV k="Left for spending & savings"
              v={money(d.totals.income_monthly - d.totals.bills_monthly,
                       false)}
              tone={d.totals.income_monthly - d.totals.bills_monthly >= 0
                      ? "good" : "bad"} />
          <KV k="Due this week"
              v={`${money(d.totals.week_total, false)} · ${d.totals.week_count} bill${d.totals.week_count === 1 ? "" : "s"}`} />
          {d.proposals.length > 0 && (
            <KV k="Proposals to review" v={String(d.proposals.length)}
                tone="warn" />)}
        </Card>
      )}
      {/* the triage queue (mirrors the web's Bills banner): one line per
          flagged bill — the story the ledger tells plus the ONE action the
          server's evidence supports (`suggestion`, rendered verbatim). A
          weak signal degrades to Review; every action is reversible and
          offers Undo, so nothing destructive rides one tap. */}
      {d && d.bills.some(attnRow) && (
        <Card>
          {d.bills.filter(attnRow).map((b) => (
            <AttentionLine key={b.payee} b={b} viewer={viewer}
              busy={(disable.isPending
                      && disable.variables?.payee === b.payee)
                    || (accept.isPending
                        && accept.variables?.b.payee === b.payee)
                    || (hint.isPending
                        && hint.variables?.payee === b.payee)}
              onReview={() => router.push({
                pathname: "/bill-history",
                params: { payee: b.payee } } as never)}
              onAccept={(amt) => accept.mutate({ b, amt })}
              onDisable={() => {
                // the toggle door is a true FLIP — a double-fire while
                // the first is in flight would silently net to no change
                if (disable.isPending) return;
                disable.mutate({ payee: b.payee, disabled: true });
              }}
              onDismiss={() => onHint?.(b.payee, "dismiss", "health")} />
          ))}
          {undoAct && (
            <View style={{ flexDirection: "row", gap: 12, marginTop: 6,
                           alignItems: "center" }}>
              <Text style={s.mut}>{undoAct.label}</Text>
              <Text style={{ color: C.accent, fontSize: 12,
                             opacity: disable.isPending || accept.isPending
                               ? 0.4 : 1 }}
                    onPress={() => {
                      if (disable.isPending || accept.isPending) return;
                      undoAct.act(); setUndoAct(null); }}>
                Undo</Text>
              <Text style={{ color: C.mut, fontSize: 12 }}
                    onPress={() => setUndoAct(null)}>✕</Text>
            </View>
          )}
        </Card>
      )}
      {notice && (
        <Text style={[s.mut, { textAlign: "center" }]}>{notice}</Text>
      )}
      {d && d.proposals.length > 0 && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "center",
                         justifyContent: "space-between", gap: 8 }}>
            <H>Proposals</H>
            {/* one click for the common case, as on the web — the
                finder's first pass over a fresh history is usually right */}
            {!viewer && d.proposals.length > 1 && (
              <Pressable style={s.pBtn}
                         disabled={approveAll.isPending || act.isPending}
                         onPress={() => approveAll.mutate()}>
                <Text style={s.pBtnText}>
                  {approveAll.isPending ? "Approving…"
                    : `Approve all ${d.proposals.length}`}
                </Text>
              </Pressable>
            )}
          </View>
          <Text style={s.mut}>
            {d.proposals.length} detected pattern
            {d.proposals.length === 1 ? "" : "s"} awaiting review.
          </Text>
          {d.proposals.map((p) => {
            // this row's own decision in flight — never the whole list
            const busy = act.isPending && act.variables?.pid === p.id;
            return (
            <View key={p.id} style={s.proposal}>
              <View style={{ flex: 1 }}>
                <View style={{ flexDirection: "row", alignItems: "center",
                               gap: 6 }}>
                  <Text style={[s.payee, { flexShrink: 1,
                                           color: C.accent }]}
                        numberOfLines={1}
                        onPress={() => router.push({
                          pathname: "/bill-history",
                          params: { payee: p.payee } } as never)}>
                    {p.payee}
                  </Text>
                  {/* kind pill always, in the web's tones: remove is
                      red, add is green, drift stays amber */}
                  {p.kind ? (
                    <Pill text={p.kind === "attach" ? "fee"
                            : p.kind === "merchant" ? "add merchant" : p.kind}
                          tone={p.kind === "remove" ? "bad"
                            : p.kind === "add" ? "good" : "warn"} />
                  ) : null}
                  {p.kind === "attach" && p.bill_payee ? (
                    <Text style={{ color: C.mut, fontSize: 12 }}>
                      a fee of {p.bill_payee}</Text>) : null}
                  {p.kind === "merchant" && p.bill_payee ? (
                    <Text style={{ color: C.mut, fontSize: 12 }}>
                      add to bill “{p.bill_payee}”</Text>) : null}
                  {p.bill_type === "envelope"
                    ? <Pill text="envelope" /> : null}
                  {p.income ? <Pill text="income" tone="good" /> : null}
                  {p.llm_tag ? <Pill text={p.llm_tag} /> : null}
                </View>
                <Text style={s.mut}>
                  {money(p.amount)} · {p.cadence}
                  {p.next_due ? ` · next ${mmddyy(p.next_due)}` : ""}
                  {p.summary ? ` · ${p.summary}` : ""}
                </Text>
              </View>
              {!viewer && (<>
              <Pressable style={[s.pBtn, busy && { opacity: 0.5 }]}
                         disabled={busy}
                         onPress={() => act.mutate({ pid: p.id,
                                                     action: "approve" })}>
                <Text style={s.pBtnText}>Approve</Text>
              </Pressable>
              <Pressable style={[s.pBtn, s.pBtnQuiet, busy && { opacity: 0.5 }]}
                         disabled={busy}
                         onPress={() => act.mutate({ pid: p.id,
                                                     action: "reject" })}>
                <Text style={[s.pBtnText, { color: C.mut }]}>Reject</Text>
              </Pressable>
              </>)}
            </View>
            );
          })}
          {act.isError ? (
            <Text style={{ color: C.bad, fontSize: 12 }}>
              {String(act.error)}
            </Text>
          ) : null}
        </Card>
      )}
      {/* income first, like the web page — money in before money out.
          A row opens the payee's HISTORY (the web's name link); the
          editor lives one tap further, like the web's ✎ expander. */}
      {income.length > 0 && (
        <Card>
          <H>Income</H>
          {income.map((b) => (
            <BillRow key={b.payee} b={b} onHint={onHint}
              onPress={() => router.push({ pathname: "/bill-history",
                params: { payee: b.payee } } as never)} />
          ))}
        </Card>
      )}
      {GROUP_ORDER.filter((g) => !g.includes("envelope")).map((g) => {
        const list = billsByGroup.get(g);
        if (!list?.length) return null;
        return (
          <Card key={g}>
            <H>{g}</H>
            {list.map((b) => (
              <BillRow key={b.payee} b={b} onHint={onHint}
                onPress={() => router.push({ pathname: "/bill-history",
                    params: { payee: b.payee } } as never)} />
            ))}
          </Card>
        );
      })}
      {/* envelopes are their OWN section, like the web — pooled caps
          behave differently and the subtitle says how */}
      {(() => {
        const monthly = billsByGroup.get("Monthly envelope") ?? [];
        const annual = billsByGroup.get("Annual envelope") ?? [];
        if (!monthly.length && !annual.length) return null;
        return (
          <Card>
            <H>Envelopes</H>
            <Text style={[s.mut, { textAlign: "left", paddingTop: 0 }]}>
              pooled caps; overflow counts as variable spending
            </Text>
            {monthly.length > 0 && (
              <Text style={{ color: C.mut, fontSize: 11,
                             fontWeight: "700", marginTop: 6,
                             textTransform: "uppercase" }}>
                Monthly pools
              </Text>
            )}
            {monthly.map((b) => (
              <BillRow key={b.payee} b={b} onHint={onHint}
                onPress={() => router.push({ pathname: "/bill-history",
                    params: { payee: b.payee } } as never)} />
            ))}
            {annual.length > 0 && (
              <Text style={{ color: C.mut, fontSize: 11,
                             fontWeight: "700", marginTop: 6,
                             textTransform: "uppercase" }}>
                Annual pools
              </Text>
            )}
            {annual.map((b) => (
              <BillRow key={b.payee} b={b} onHint={onHint}
                onPress={() => router.push({ pathname: "/bill-history",
                    params: { payee: b.payee } } as never)} />
            ))}
          </Card>
        );
      })()}
      {d && bills.length > 0 && (
        <Text style={[s.mut, { textAlign: "center", marginTop: 4 }]}>
          A healthy bill carries no status pill — a quiet row is the ✓.
        </Text>
      )}
      {d && d.bills.length === 0 && d.proposals.length === 0
        && !query.isPending && (
        <Text style={s.center}>
          No recurring bills yet. Import or connect your bank, then hit
          Find recurring bills — or add one by hand.
        </Text>
      )}
      {(d?.archived ?? []).length > 0 && (
        <Card>
          <Pressable onPress={() => setShowArch((x) => !x)}>
            <Text style={[s.mut, { fontWeight: "600" }]}>
              {showArch ? "▾" : "▸"} Archived — {d!.archived.length}{" "}
              removed bill{d!.archived.length === 1 ? "" : "s"} (history
              kept)
            </Text>
          </Pressable>
          {showArch && d!.archived.map((b) => (
            <View key={b.payee} style={s.proposal}>
              <View style={{ flex: 1 }}>
                <Text style={[s.payee, { color: C.accent }]}
                      onPress={() => router.push({
                        pathname: "/bill-history",
                        params: { payee: b.payee } } as never)}>
                  {b.payee}
                </Text>
                <Text style={s.mut}>
                  {money(Math.abs(b.amount))}
                  {b.frequency ? ` · ${b.frequency}` : ""}
                  {b.synced_at
                    ? ` · archived ${mmddyy(b.synced_at)}` : ""}
                </Text>
              </View>
              {!viewer && (() => {
                const restoring = restore.isPending
                  && restore.variables === b.payee;
                const deleting = delForever.isPending
                  && delForever.variables === b.payee;
                return (<>
                <Pressable style={[s.pBtn, s.pBtnQuiet,
                             restoring && { opacity: 0.5 }]}
                           disabled={restoring}
                           onPress={() => restore.mutate(b.payee)}>
                  <Text style={[s.pBtnText, { color: C.mut }]}>
                    {restoring ? "restoring…" : "Restore"}
                  </Text>
                </Pressable>
                <Text style={[{ color: C.bad, fontSize: 12, padding: 4 },
                               deleting && { opacity: 0.5 }]}
                      onPress={() => deleting || Alert.alert(
                        "Delete forever",
                        "Permanently delete this archived bill? The "
                        + "ledger transactions stay; only this bill "
                        + "record is removed.",
                        [{ text: "Delete forever", style: "destructive",
                           onPress: () => delForever.mutate(b.payee) },
                         { text: "Cancel", style: "cancel" }])}>
                  {deleting ? "deleting…" : "delete forever"}
                </Text>
                </>);
              })()}
            </View>
          ))}
        </Card>
      )}

      {/* the 5-week calendar closes the page, like the web's Bills —
          gated on the UNFILTERED list (the web's rule): an income-only
          household still has paychecks to show on the grid */}
      {(d?.bills ?? []).length > 0 && <CalendarSection />}

      <Sheet visible={adding} onClose={() => setAdding(false)}
             title="Add a bill">
            {/* the draft's own fields live here (keyed on `adding`) — a
                keystroke used to re-render the whole tab behind the sheet */}
            {adding && (
              <AddBillFields pending={add.isPending}
                             error={add.isError ? String(add.error) : null}
                             onAdd={(f) => add.mutate(f)}
                             onCancel={() => setAdding(false)} />
            )}
      </Sheet>
      <HelpLink topic="bills" />
    </ScrollView>
  );
}

type AddBillForm = { payee: string; amount: string; cadence: string;
                     due: string; income: boolean; pinned: boolean;
                     category: string; isEnv: boolean };

function AddBillFields({ pending, error, onAdd, onCancel }: {
  pending: boolean; error: string | null;
  onAdd: (f: AddBillForm) => void; onCancel: () => void;
}) {
  const { client } = useSession();
  const [nPayee, setNPayee] = useState("");
  const [nAmount, setNAmount] = useState("");
  const [nCadence, setNCadence] = useState("MONTHLY:1");
  const [nCustomN, setNCustomN] = useState("2");
  const [nCustomUnit, setNCustomUnit] =
    useState<"MONTHLY" | "YEARLY">("YEARLY");
  const [nDue, setNDue] = useState("");
  const [nIncome, setNIncome] = useState(false);
  const [nPinned, setNPinned] = useState(false);
  const [nCategory, setNCategory] = useState("");
  // known ledger merchants — picking one makes the bill match its past
  // and future transactions (the web datalist)
  const merchants = useQuery({ queryKey: ["merchants"],
    queryFn: () => client!.merchants(), enabled: !!client });
  const effCadence = nCadence === "CUSTOM"
    ? `${nCustomUnit}:${Math.max(1, parseInt(nCustomN, 10) || 1)}`
    : nCadence;
  const nIsEnv = effCadence.startsWith("ENVELOPE");
  // known categories feed the envelope-category field (the web
  // datalist) — free text stays possible, typos stop being the default
  const catsQ = useQuery({ queryKey: ["categories"],
    queryFn: () => client!.categories(), enabled: !!client });
  const payeeSuggestions = nPayee.trim().length > 1
    ? (merchants.data?.merchants ?? [])
        .filter((m) => m.toLowerCase()
          .includes(nPayee.trim().toLowerCase())
          && m.toLowerCase() !== nPayee.trim().toLowerCase())
        .slice(0, 5)
    : [];
  return (
    <>
      <TextInput style={s.input}
                 placeholder="merchant — pick a known one or type a new name"
                 placeholderTextColor={C.mut} value={nPayee}
                 onChangeText={setNPayee} />
      {payeeSuggestions.length > 0 && (
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
          {payeeSuggestions.map((m) => (
            <Pressable key={m} style={s.chip} onPress={() => setNPayee(m)}>
              <Text style={{ color: C.accent, fontSize: 12 }}>{m}</Text>
            </Pressable>
          ))}
        </View>
      )}
      <TextInput style={s.input} placeholder="amount"
                 placeholderTextColor={C.mut} value={nAmount}
                 onChangeText={setNAmount} keyboardType="decimal-pad" />
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
        {/* FREQ:interval values, the same wire format the web
            add-form sends — never the human label. The list is the
            web's SIMPLE_CADENCES verbatim (+ Custom): no invented
            options, and daily exists here too. */}
        {([["DAILY:1", "daily"], ["WEEKLY:1", "weekly"],
           ["MONTHLY:1", "monthly"], ["YEARLY:1", "yearly"],
           ["ENVELOPE:1", "envelope (monthly pool)"],
           ["ENVELOPE:12", "envelope (annual pool)"],
           ["CUSTOM", "custom interval…"],
          ] as [string, string][]).map(([v, l]) => (
          <Pressable key={v}
                     style={[s.chip, nCadence === v && s.chipOn]}
                     onPress={() => setNCadence(v)}>
            <Text style={{ color: nCadence === v ? C.text : C.mut,
                           fontSize: 12 }}>{l}</Text>
          </Pressable>
        ))}
      </View>
      {nCadence === "CUSTOM" && (
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 6 }}>
          <Text style={{ color: C.mut, fontSize: 12 }}>every</Text>
          <TextInput style={[s.input, { width: 52 }]}
                     keyboardType="number-pad" value={nCustomN}
                     onChangeText={setNCustomN} />
          {(["MONTHLY", "YEARLY"] as const).map((u) => (
            <Pressable key={u}
                       style={[s.chip, nCustomUnit === u && s.chipOn]}
                       onPress={() => setNCustomUnit(u)}>
              <Text style={{ color: nCustomUnit === u
                               ? C.text : C.mut, fontSize: 12 }}>
                {u === "MONTHLY" ? "month(s)" : "year(s)"}
              </Text>
            </Pressable>
          ))}
        </View>
      )}
      {nIsEnv && (
        <>
          <TextInput style={s.input}
                     placeholder="envelope category (optional — counts that category's spend toward the pool)"
                     placeholderTextColor={C.mut}
                     autoCapitalize="none"
                     value={nCategory} onChangeText={setNCategory} />
          {/* known categories as chips (the web datalist) — free
              text stays possible, typos stop being the default */}
          {nCategory.trim().length > 0 && (
            <View style={{ flexDirection: "row", flexWrap: "wrap",
                           gap: 6 }}>
              {/* matched on the DISPLAY label the chip renders below —
                  filtering the raw constant meant typing what you could
                  see ("general services") offered nothing */}
              {filterCategories(
                [...new Set([...(catsQ.data?.categories ?? []),
                             ...(catsQ.data?.plaid_spend ?? [])])],
                nCategory)
                .filter((c) => c !== nCategory)
                .slice(0, 8)
                .map((c) => (
                  <Pressable key={c} style={s.chip}
                             onPress={() => setNCategory(c)}>
                    <Text style={{ color: C.accent, fontSize: 12 }}>
                      {catLabel(c)}
                    </Text>
                  </Pressable>
                ))}
            </View>
          )}
        </>
      )}
      <TextInput style={s.input}
                 placeholder="next due (YYYY-MM-DD, optional)"
                 placeholderTextColor={C.mut} value={nDue}
                 onChangeText={setNDue} autoCapitalize="none" />
      <View style={{ flexDirection: "row", alignItems: "center",
                     gap: 8 }}>
        <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
          This is income
        </Text>
        <Switch value={nIncome} onValueChange={setNIncome}
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
      {!nIncome && (
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 8 }}>
          <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
            Pin to Today (its own card + daily email)
          </Text>
          <Switch value={nPinned} onValueChange={setNPinned}
                  trackColor={{ true: C.accent, false: C.hover }}
                  thumbColor={C.text} />
        </View>
      )}
      {error ? (
        <Text style={{ color: C.bad, fontSize: 12 }}>{error}</Text>
      ) : null}
      <View style={{ flexDirection: "row", gap: 8,
                     justifyContent: "center" }}>
        <Pressable style={[s.pBtn,
                     (!nPayee.trim() || !nAmount || pending)
                       && { opacity: 0.5 }]}
                   disabled={!nPayee.trim() || !nAmount || pending}
                   onPress={() => onAdd({ payee: nPayee, amount: nAmount,
                     cadence: effCadence, due: nDue, income: nIncome,
                     pinned: nPinned, category: nCategory,
                     isEnv: nIsEnv })}>
          <Text style={s.pBtnText}>Add</Text>
        </Pressable>
        <Pressable style={[s.pBtn, s.pBtnQuiet]} onPress={onCancel}>
          <Text style={[s.pBtnText, { color: C.mut }]}>Cancel</Text>
        </Pressable>
      </View>
    </>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  bill: { borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
          paddingVertical: 8 },
  billTop: { flexDirection: "row", justifyContent: "space-between", gap: 8 },
  billSub: { flexDirection: "row", justifyContent: "space-between",
             alignItems: "center", gap: 8, marginTop: 2 },
  payee: { color: C.text, fontSize: 14, fontWeight: "600", flexShrink: 1 },
  amount: { color: C.text, fontSize: 14, fontVariant: ["tabular-nums"] },
  mut: { color: C.mut, fontSize: 12 },
  proposal: { flexDirection: "row", alignItems: "center", gap: 8,
              borderTopColor: C.border,
              borderTopWidth: StyleSheet.hairlineWidth,
              paddingVertical: 8 },
  pBtn: { backgroundColor: C.accent, borderRadius: 8,
          paddingHorizontal: 12, paddingVertical: 7 },
  pBtnQuiet: { backgroundColor: C.hover },
  pBtnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  toolRow: { flexDirection: "row", gap: 8, marginHorizontal: 12,
             marginTop: 10 },
  tool: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 14, paddingVertical: 7 },
  toolText: { color: C.accent, fontSize: 13, fontWeight: "600" },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 15,
           paddingHorizontal: 10, paddingVertical: 8 },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
});

// A bill the triage queue lists — same rule the server's attention count
// uses (mirrors webapp's attnRows filter).
function attnRow(b: Bill): boolean {
  return ["stale", "mismatch", "misdated", "drifting"]
    .includes(b.health ?? "") && !b.health_dismissed && !b.disabled;
}

// One line of the triage queue: severity dot, the story the ledger tells,
// and the ONE action the server's evidence supports (copy mirrors the
// web's AttentionLine — the client never invents an action or a number).
function AttentionLine({ b, viewer, busy, onReview, onAccept, onDisable,
                         onDismiss }: {
  b: Bill; viewer: boolean; busy: boolean; onReview: () => void;
  onAccept: (amt: number) => void; onDisable: () => void;
  onDismiss: () => void;
}) {
  const sug = b.suggestion;
  const plusDays = (iso: string, days: number) => {
    const t = new Date(iso + "T00:00:00Z");
    t.setUTCDate(t.getUTCDate() + days);
    return t.toISOString().slice(0, 10);
  };
  // the stale verdict is judged on ANY charge from the merchant, so the
  // story's "nothing since" must be the newest one (mirrors web)
  const lastSeen = b.last_seen?.date
    && (!b.last_match || b.last_seen.date > b.last_match)
    ? b.last_seen.date : b.last_match;
  const story =
    b.health === "stale"
      ? `missed its${lastSeen && b.cycle_days
          ? ` ~${mmddyy(plusDays(lastSeen, b.cycle_days))}` : ""}` +
        ` cycle — ${lastSeen
          ? `nothing since ${mmddyy(lastSeen)}`
          : "no matching charge on record"}`
    : b.health === "mismatch"
      ? (sug?.action === "accept_amount" && sug.amount !== undefined
          ? `charges now run ${money(sug.amount)}, bill says ${money(b.amount)}`
          : `charges near ${b.last_seen ? money(b.last_seen.amount) : "?"}` +
            `${b.last_seen ? ` on ${mmddyy(b.last_seen.date)}` : ""}, ` +
            `bill says ${money(b.amount)} — amounts vary`)
    : b.health === "drifting"
      ? "amount wandering — auto-corrects tonight"
      : "payments match, but the due-date anchor is off the real draft day";
  const btn = (label: string, onPress: () => void, primary = false) => (
    <Pressable onPress={onPress} disabled={busy}
        style={{ paddingVertical: 3, paddingHorizontal: 9, borderRadius: 7,
                 borderWidth: 1, opacity: busy ? 0.5 : 1,
                 borderColor: primary ? C.accent : C.border,
                 backgroundColor: primary ? C.hover : "transparent" }}>
      <Text style={{ fontSize: 12,
                     color: primary ? C.accent : C.mut }}>{label}</Text>
    </Pressable>
  );
  return (
    <View style={{ paddingVertical: 6, gap: 6 }}>
      <Pressable onPress={onReview}
                 style={{ flexDirection: "row", gap: 8,
                          alignItems: "center" }}>
        <View style={{ width: 8, height: 8, borderRadius: 4,
                       backgroundColor: b.health === "stale"
                         ? C.bad : C.warn }} />
        <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
          <Text style={{ fontWeight: "600" }}>{b.payee}</Text> {story}
        </Text>
      </Pressable>
      {!viewer && (
        <View style={{ flexDirection: "row", gap: 8, paddingLeft: 16,
                       flexWrap: "wrap" }}>
          {sug?.action === "accept_amount" && sug.amount !== undefined
            && btn(`Accept ${money(sug.amount)}`,
                   () => onAccept(sug.amount!), true)}
          {sug?.action === "disable"
            && btn("Disable bill", onDisable, true)}
          {(!sug || sug.action === "review")
            && btn("Review…", onReview, true)}
          {btn("Dismiss", onDismiss)}
        </View>
      )}
    </View>
  );
}
