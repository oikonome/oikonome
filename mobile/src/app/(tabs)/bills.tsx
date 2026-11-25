// Bills — the recurring schedule, read-only. Mirrors the web page's
// anatomy: headline totals, attention count, proposals nudge, then the
// active schedule with per-bill state (envelopes show their pool).
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, Pressable, ScrollView, StyleSheet, Switch, Text,
         TextInput, View } from "react-native";
import PullRefresh from "../../components/pull-refresh";
import Sheet from "../../components/sheet";

import CalendarSection from "../../components/calendar-section";
import { EmptyState, FailedLine, StaleLine } from "../../components/loaded";
import { OfflineNote, offlineGrey, useOffline } from "../../components/offline";
import { Card, H, HelpLink, KV, Meter, Pill, Skeleton, Note } from "../../components/ui";
import { ApiError, errText, Bill, BillsData, Proposal, SettingsView }
  from "../../lib/api";
import { patchDismissedHint, patchList, patchQuery, removeFromList,
         SETTINGS_SAVES, saveSettings } from "../../lib/cache";
import { useRouter, useScrollToTop } from "expo-router";
import { useMemo, useRef, useState } from "react";

import { useRefetchWhenShown } from "../../lib/focus";
import { onSeveralDays } from "../../lib/monthdays";
import { useSession } from "../../lib/session";

const BILLS_KEY = ["bills"];

// ---- stepping back through the months (mirrors the web page) ----
const ymOf = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
export const shiftYm = (ym: string, by: number) => {
  const [y, m] = ym.split("-").map(Number);
  return ymOf(new Date(y, m - 1 + by, 1));
};
export const ymLabel = (ym: string) => {
  const [y, m] = ym.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(undefined, { month: "long", year: "numeric" });
};
import { useMe, useOwner, useViewer } from "../../lib/viewer";
import { C, mmddyy, money } from "../../lib/theme";
import { disclosure } from "../../lib/a11y";
import { ADD_CADENCES, CUSTOM_UNITS, catLabel, customCadence, customMax,
         filterCategories, missedBillPill,
         proposalSuperseded, scanAlreadyRunning,
         SCAN_RUNNING_RETRY_MS, householdDay, EMPTY_LINES, approveAllWords,
         billCycleDays }
  from "../../lib/pure";
import { fieldA11y } from "../../components/field";

// the web row's status column, ported whole: a quiet row IS the ✓ —
// only a bill wanting a decision says anything
type DueWords = { label: string | null; text: string | null;
                  tone: Bill["due_tone"]; frac: number | null };
const DAY = 86_400_000;
// A server older than the row's due words: the device counts them from
// the household's day (/api/me timezone), as that server's web did.
function olderDueWords(b: Bill, zone: string | undefined): DueWords {
  if (!b.due_on) return { label: null, text: null, tone: null, frac: null };
  const today = householdDay(new Date(), zone);
  const n = Math.round(
    (new Date(b.due_on + "T00:00:00").getTime() - today.getTime()) / DAY);
  const cyc = onSeveralDays(b.by_month_day)
    // the row's cadence value, as the web reads it — never the label
    ? billCycleDays("MONTHLY:1") / b.by_month_day.length
    : billCycleDays(b.cadence_value);
  const frac = cyc <= 0 ? 0 : Math.min(1, Math.max(0, 1 - n / cyc));
  const label = n === 0 ? "today" : n === 1 ? "tomorrow" : mmddyy(b.due_on);
  if (b.occurrences > 0 && b.paid >= b.occurrences)
    return { label, text: null, tone: null, frac };
  const text = n === 0 ? (b.income ? "today" : "due today")
    : n === 1 ? (b.income ? "tomorrow" : "due tomorrow")
    : n === -1 ? "1 day late"
    : n < 0 ? `${-n} days late`
    : `in ${n} days`;
  const tone = n < 0 ? (b.withdrawn_on ? "withdrawn" : "late")
    : n <= 3 ? "soon" : "ok";
  return { label, text, tone, frac };
}
// only these are problems worth a pill — off-ledger is a way of life
// for cash/manual bills, and ok is silence
const HEALTH_PILL: Record<string, "warn" | "bad"> = {
  drifting: "warn", stale: "bad", mismatch: "warn", misdated: "warn",
};

function BillRow({ b, onPress, onHint, past }: {
  b: Bill; onPress?: () => void;
  // a past month on view: no countdown — paid, missed, or not due
  past?: boolean;
  // dismiss/restore a flag from the row (web's ✕ / ↺) — absent = viewer
  onHint?: (payee: string, action: "dismiss" | "restore",
            target: "fit" | "health") => void;
}) {
  const envelope = b.used !== null;
  const isAnnualEnv = envelope && b.period_months === 12;
  const zone = useMe().data?.timezone;
  // the row's due date, countdown, its colour and the cycle bar's fill are
  // the server's (api_bills.due_words), as on the web
  const due: DueWords = b.due_text === undefined
    // older-server path: stands in for `due_label` / `due_text` / `due_tone` / `cycle_frac`
    ? (past || envelope ? { label: null, text: null, tone: null, frac: null }
                        : olderDueWords(b, zone))
    : { label: b.due_label ?? null, text: b.due_text, tone: b.due_tone ?? null,
        frac: b.cycle_frac ?? null };
  const settled = b.occurrences > 0 && b.paid >= b.occurrences;
  const missed = !!past && !settled && b.occurrences > 0;
  const overdue = due.tone === "late" || due.tone === "withdrawn";
  const soon = due.tone === "soon";
  const cycleFrac = due.frac;

  let status: React.ReactNode;
  if (envelope) {
    // what is left of the pool is the server's (period_left), as on the
    // web; an older server sends none and the word is left out
    const left = b.period_left;
    status = (b.overflow ?? 0) > 0.005
      ? <Pill text={`over +${money(b.overflow!, false)}`} tone="bad" />
      : left == null ? null
      : left <= b.amount * 0.001
        ? <Pill text={`used up${isAnnualEnv ? " this year" : ""}`} tone="warn" />
        : <Text style={s.mut}>
            {money(left, false)} left{isAnnualEnv ? " this year" : ""}
          </Text>;
  } else if (past && !settled) {
    // the month on view is over: paid, missed, or never due
    status = missed
      ? (b.withdrawn_on
          ? <Pill text="withdrawn" tone="warn" />
          : <Pill text={b.income ? "not received" : "missed"} tone="bad" />)
      : <Text style={s.mut}>not due</Text>;
  } else if (b.income) {
    // the paid words are the server's ("received 05/15", "1 of 2
    // received"); an older server sends none and they are left out
    status = settled
      ? <Text style={{ color: C.good, fontSize: 12 }}>{b.paid_text ?? ""}</Text>
      : due.text
        ? <Text style={[s.mut, { color: soon ? C.warn : C.good }]}>
            {b.paid_text ? `${b.paid_text} · ` : ""}{due.text}</Text>
        : <Pill text="awaiting" />;
  } else {
    status = settled
      ? <Text style={s.mut}>{b.paid_text ?? ""}</Text>
      : overdue
        // a charge the bank feed withdrew is a warning, not a missed payment
        ? <Pill text={missedBillPill(b.withdrawn_on).text}
                tone={missedBillPill(b.withdrawn_on).tone} />
        : due.text
          ? <Text style={[s.mut, { color: soon ? C.warn : C.good }]}>
              {b.paid_text ? `${b.paid_text} · ` : ""}{due.text}</Text>
          : <Pill text="awaiting" />;
  }
  // the server's sum of the bill and its fees (amount_with_fees); an
  // older server sends none and the row prints no amount
  const amountText = (b.income ? "+" : "")
    // older-server path: stands in for `amount_with_fees`
    + money(b.amount_with_fees ?? b.amount + (b.fee_total || 0));
  // a past month on view prints the server's unpaid date for that month
  // (next_unpaid), the date the web's "Due" column shows for the same bill
  const when = b.cadence + (past
    ? (b.next_unpaid ? ` · due ${mmddyy(b.next_unpaid)}` : "")
    : due.label ? ` · due ${due.label}`
    : b.next_unpaid ? ` · next ${mmddyy(b.next_unpaid)}` : "");
  // The row's press target lies BENEATH its content, as a sibling, so the
  // ✕ and ↺ flags on the row are buttons of their own a screen reader can
  // reach (a touchable inside another is reachable only through the outer
  // one). The content passes touches through to it; the flags take
  // theirs. The target speaks the row; status pills follow it.
  const hidden = { importantForAccessibility: "no-hide-descendants" as const,
                   accessibilityElementsHidden: true };
  return (
    <View style={s.bill}>
      <Pressable style={({ pressed }) => [s.billPress,
                   pressed && onPress ? { backgroundColor: C.hover } : null]}
                 onPress={onPress} disabled={!onPress}
                 accessibilityRole="button"
                 accessibilityLabel={`${b.payee}, ${amountText}${b.fee_total
                   ? ` incl. ${money(b.fee_total)} fee` : ""}, ${when}`} />
      <View style={s.billTop} pointerEvents="none" {...hidden}>
        <Text style={s.payee} numberOfLines={1}>{b.payee}</Text>
        <Text style={[s.amount, b.income && { color: C.good }]}>
          {amountText}
          {b.fee_total
            ? <Text style={{ color: C.mut, fontSize: 11 }}>
                {"  incl. "}{money(b.fee_total)}{" fee"}</Text> : null}
        </Text>
      </View>
      <View style={s.billSub} pointerEvents="box-none">
        <View pointerEvents="none" {...hidden}>
          <Text style={s.mut}>{when}</Text>
        </View>
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 6 }} pointerEvents="box-none">
          {(status || b.disabled) && (
            <View style={s.passRow} pointerEvents="none">
              {status}
              {b.disabled && <Pill text="disabled" />}
            </View>
          )}
          {!b.disabled && b.health && !b.health_dismissed
            && HEALTH_PILL[b.health] ? (
            <>
              <View pointerEvents="none">
                <Pill text={b.health === "mismatch" && b.last_seen
                  ? `mismatch · ledger ${money(b.last_seen.amount)} on ${mmddyy(b.last_seen.date)}`
                  : b.health} tone={HEALTH_PILL[b.health]} />
              </View>
              {/* dismiss — it returns only if the problem changes */}
              {onHint && (
                <Pressable accessibilityRole="button" accessibilityLabel="Dismiss"
                           onPress={() => onHint(b.payee, "dismiss", "health")}>
                  <Text style={{ color: C.mut, fontSize: 14, paddingHorizontal: 14,
                                 paddingVertical: 12 }}>✕</Text>
                </Pressable>
              )}
            </>
          ) : null}
          {/* the missed cycle's charge was withdrawn by the bank feed: a
              warning, not a problem to decide — no dismiss, not in the
              triage queue, and not repeated when the status already says
              it. Gated on this month's own reading (withdrawn_on), never
              the health verdict's earlier cycle. The bill screen spells
              out the date (missedBillPill). */}
          {!b.disabled && b.health === "withdrawn"
            && !(overdue && b.withdrawn_on) ? (
            <View pointerEvents="none"><Pill text="withdrawn" tone="warn" /></View>
          ) : null}
          {/* the way back from a dismissed health flag */}
          {!b.disabled && b.health && b.health_dismissed
            && HEALTH_PILL[b.health] && onHint ? (
            <Pressable accessibilityRole="button" accessibilityLabel="Restore this flag"
                       hitSlop={10}
                       onPress={() => onHint(b.payee, "restore", "health")}>
              <Text style={{ color: C.mut, fontSize: 12 }}>↺</Text>
            </Pressable>
          ) : null}
          {/* the ledger disagrees with this bill — the 💡 opens the
              history, where "edit with these values" applies the fit */}
          {!b.disabled && b.fit && !b.hint_dismissed
            && (["mismatch", "stale", "misdated"].includes(b.health ?? "")
                || b.fit_diverges)
            ? (
            <>
              <View pointerEvents="none">
                <Text style={{ fontSize: 12 }}
                      accessibilityLabel="The ledger disagrees with this bill">💡</Text>
              </View>
              {onHint && (
                <Pressable accessibilityRole="button" accessibilityLabel="Dismiss this hint"
                           hitSlop={10}
                           onPress={() => onHint(b.payee, "dismiss", "fit")}>
                  <Text style={{ color: C.mut, fontSize: 12 }}>✕</Text>
                </Pressable>
              )}
            </>
          ) : null}
        </View>
      </View>
      <View pointerEvents="none">
        {/* the envelope's fill is the server's period_frac, the web's own
            bar; a server without it draws none */}
        {envelope && (b.period_frac != null
                      || (b.period_frac === undefined && !!b.pool)) ? (
          // older-server path: stands in for `period_frac`
          <Meter frac={b.period_frac ?? (b.period_used ?? 0) / (b.pool || 1)}
                 tone={b.overflow ? "bad" : undefined} />
        ) : cycleFrac !== null && !settled ? (
          <Meter frac={cycleFrac} tone={overdue ? "bad" : soon ? "warn" : undefined} />
        ) : null}
      </View>
    </View>
  );
}

// the web page's cadence groups, in its rank order — every interval gets
// its own heading instead of an "Other" catch-all
const GROUP_ORDER = ["Weekly", "Every 14 days", "Every 2 weeks",
                     "Twice a month", "Monthly",
                     "Monthly envelope", "Annual envelope",
                     "Every 8 weeks", "Every 2 months", "Every 3 months",
                     "Every 6 months", "Yearly", "Every 2 years",
                     "One-time / other"];
function cadenceGroup(b: Bill): string {
  if (b.used !== null)
    return b.period_months === 12 ? "Annual envelope" : "Monthly envelope";
  if (onSeveralDays(b.by_month_day)) return "Twice a month";
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

// The nightly finder's on/off (the web Bills page's switch, same words).
// Choosing "I'll enter them myself" in the wizard turns it off; this is
// the one place to turn it back on, so it sits beside the Find button.
// The setting is the owner's (the server refuses anyone else with a 403),
// so everyone else reads where it stands instead of a switch that snaps
// back.
function NightlyFinderSwitch({ onError }: { onError: (m: string) => void }) {
  const owner = useOwner();
  const { client } = useSession();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const save = useMutation({
    scope: SETTINGS_SAVES,
    mutationFn: (on: boolean) =>
      saveSettings(qc, client!, { bill_detection_auto: on }),
    // the switch flips on the tap; saveSettings then seeds the cache with
    // the server's full view, and a failed write refetches the stored one
    onMutate: (on) => patchQuery<SettingsView>(qc, ["settings"],
                                               { bill_detection_auto: on }),
    onError: (e) => {
      onError(errText(e));
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
  // absent means on — only an explicit false stops it
  // An unread setting is neither on nor off: nothing while it loads, the
  // failed line when it failed — never a switch that reads "on" by default.
  if (settings.data === undefined)
    return settings.isError
      ? <FailedLine q={settings} what="the nightly search setting" /> : null;
  const on = settings.data.bill_detection_auto !== false;
  if (!owner)
    return settings.data ? (
      <View style={{ marginHorizontal: 12, marginTop: 8 }}>
        <Text style={{ color: C.text, fontSize: 14 }}>
          Nightly search for new bills and paychecks: {on ? "on" : "off"}
        </Text>
        <Text style={[s.mut, { marginTop: 2 }]}>
          the household owner sets this
        </Text>
      </View>
    ) : null;
  return (
    <View style={{ marginHorizontal: 12, marginTop: 8 }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
          Look for new bills and paychecks every night
        </Text>
        <Switch value={on} disabled={!settings.data || save.isPending}
                onValueChange={(v) => save.mutate(v)}
                accessibilityLabel="Look for new bills and paychecks every night"
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
      <Text style={[s.mut, { marginTop: 2 }]}>
        Off: the nightly search proposes nothing new; drift, due dates and
        change/stopped offers for your existing bills still run, and Find
        bills & income always works.
      </Text>
    </View>
  );
}

export default function Bills() {
  const { client } = useSession();
  // re-tapping the active tab scrolls back to the top
  const topRef = useRef<ScrollView>(null);
  useScrollToTop(topRef);
  const router = useRouter();
  const viewer = useViewer();
  const qc = useQueryClient();
  // offline, the writes here are greyed with "You're offline." beside them
  const offline = useOffline();
  // the month on view: null = this month (the live key every write
  // patches); a past month has its own key, so stepping back never
  // overwrites the live list in the cache
  // "this month" is the household's — the month GET /api/bills answers
  // for — so ‹ reaches the month that just closed there
  const me = useMe();
  const curYm = ymOf(householdDay(new Date(), me.data?.timezone));
  const [ym, setYm] = useState<string | null>(null);
  const isPast = ym !== null && ym < curYm;
  const queryKey = isPast ? ["bills", "month", ym] : BILLS_KEY;
  const query = useQuery({
    queryKey,
    queryFn: () => client!.bills(isPast ? ym! : undefined),
    enabled: !!client,
  });
  useRefetchWhenShown(query, queryKey);
  const shownYm = isPast ? ym! : curYm;
  // every write says what it did — silent success reads as a dead tap
  const [notice, setNotice] = useState<string | null>(null);
  // one reversible triage action at a time (the web banner's Undo)
  const [undoAct, setUndoAct] = useState<{ label: string;
                                           act: () => void } | null>(null);
  // every schedule write refreshes the 5-week calendar below AND the
  // Today tab, like the web's refetchAll — an approved bill shows up in
  // the grid, and adding one changes Today's numbers
  const refetchSchedule = () => {
    qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
    qc.invalidateQueries({ queryKey: ["calendar"], refetchType: "none" });
    qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
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
      const gone = proposalSuperseded(r);
      setNotice(gone ? `${r.payee} is already tracked — the offer was withdrawn.`
        : v.action === "approve"
        // the approve changed something the person set (a pin cleared)
        ? "Approved — the bill is live." + (r.note ? ` ${r.note}` : "")
        : "Proposal rejected.");
      dropProposal(v.pid);
      if (v.action === "approve" && !gone) {
        refetchSchedule();
        qc.invalidateQueries({ queryKey: ["payee-history", r.payee] });
      } else {
        qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
        qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
        qc.invalidateQueries({ queryKey: ["onboarding"] });
      }
      qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    },
    onError: (e, v) => {
      setNotice(errText(e));
      // an offer for a bill already tracked is withdrawn by the server as
      // it refuses: the card could neither be approved nor dismissed
      if (proposalSuperseded(e)) {
        dropProposal(v.pid);
        qc.invalidateQueries({ queryKey: ["bills"] });
      }
    },
  });
  const approveAll = useMutation({
    mutationFn: (pids: string[]) => client!.billsApproveAll(pids),
    onSuccess: (r) => {
      setNotice(approveAllWords(r)
        // an approve that cleared a pin says so, per bill
        + (r.notes?.length
          ? ` — ${r.notes.map((n) => `${n.payee}: ${n.note}`).join("; ")}` : ""));
      // only what stayed pending is still a proposal
      qc.setQueryData<BillsData>(["bills"], (old) => old && {
        ...old,
        proposals: old.proposals.filter(
          (p) => r.failed.some((f) => f.pid === p.id && !f.superseded)),
      });
      refetchSchedule();
      qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    },
    onError: (e) => Alert.alert("Approve all failed", errText(e)),
  });
  const detect = useMutation({
    mutationFn: () => client!.billsDetect(),
    onSuccess: refetchSchedule,
    onError: (e) => {
      if (scanAlreadyRunning(e)) {
        Alert.alert("Scan already running",
                    "Another scan is finding bills now — the list updates "
                    + "when it ends.");
        setTimeout(refetchSchedule, SCAN_RUNNING_RETRY_MS);
        return;
      }
      Alert.alert("Scan failed", errText(e));
    },
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
      qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
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
      qc.invalidateQueries({ queryKey: ["bills"], refetchType: "none" });
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
                          // marks this an edit of the bill the row shows,
                          // so a bill deleted since the list loaded is
                          // refused instead of recreated
                          orig_payee: b.payee,
                          // amount only: no cadence, income or next_due, so
                          // the server keeps the bill's stored values. This
                          // row may be a persisted copy older than an edit
                          // made elsewhere, and resending its cadence or
                          // direction would silently revert that edit
                        }),

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
      ...(f.restore ? { restore: true } : {}),
    }),
    onSuccess: (r, f) => {
      setNotice(r?.restored ? `Restored ${f.payee.trim()}.`
        : `Added ${f.payee.trim()}.`);
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
      <StaleLine query={query} />
      {/* step back through the months: the per-bill paid/received are
          the month on view, as it ended; the schedule stays live */}
      <View style={[s.toolRow, { alignItems: "center" }]}>
        <Pressable accessibilityRole="button" style={s.tool} accessibilityLabel="Previous month"
                   onPress={() => setYm(shiftYm(shownYm, -1))}>
          <Text style={s.toolText}>‹</Text>
        </Pressable>
        <Text style={{ color: C.text, fontWeight: "700" }}>{ymLabel(shownYm)}</Text>
        <Pressable accessibilityRole="button"
                   style={[s.tool, !isPast && { opacity: 0.4 }]} disabled={!isPast}
                   accessibilityLabel="Next month"
                   onPress={() => { const n = shiftYm(shownYm, 1); setYm(n >= curYm ? null : n); }}>
          <Text style={s.toolText}>›</Text>
        </Pressable>
        {isPast ? (
          <Pressable accessibilityRole="button" style={s.tool} onPress={() => setYm(null)}>
            <Text style={s.toolText}>This month</Text>
          </Pressable>
        ) : null}
      </View>
      {isPast ? (
        <Text style={[s.mut, { marginHorizontal: 12, marginBottom: 6 }]}>
          How the month ended: paid and received per bill. Edits change the live schedule.
        </Text>
      ) : null}
      {!viewer && (
      <View style={s.toolRow}>
        <Pressable accessibilityRole="button"
                   disabled={offline} style={[s.tool, offlineGrey(offline)]} onPress={() => setAdding(true)}>
          <Text style={s.toolText}>＋ Add a bill</Text>
        </Pressable>
        <Pressable accessibilityRole="button"
                   style={[s.tool, detect.isPending && { opacity: 0.5 }, offlineGrey(offline)]}
                   disabled={detect.isPending || offline}
                   onPress={() => detect.mutate()}>
          <Text style={s.toolText}>
            {detect.isPending ? "Scanning…" : "⌕ Find bills & income"}
          </Text>
        </Pressable>
      </View>
      )}
      {!viewer && <OfflineNote style={{ marginHorizontal: 12 }} />}
      <NightlyFinderSwitch onError={setNotice} />
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
      {query.isPending && <Skeleton />}
      {/* a failed first read says so under the stepper, which stays: the
          month that failed is one tap from the one before it */}
      {d === undefined && query.isError && (
        <FailedLine q={query} what={isPast ? `bills for ${ymLabel(shownYm)}`
                                           : "bills"} />)}
      {d?.totals && (
        <Card>
          {/* the month in the plan's terms, figure and words from the
              server (bills_hero) — the web's tiles, in the same order;
              nothing is added up here */}
          {(d.totals.hero ?? []).map((t) => (
            <View key={t.key}>
              <KV k={t.label} v={money(t.amount, false)}
                  tone={t.tone === "pos" ? "good" : t.tone === "neg" ? "bad"
                        : undefined} />
              <Text style={[s.mut, { marginTop: -2, marginBottom: 4 }]}>
                {t.note}</Text>
            </View>
          ))}
          {d.totals.week_text ? (
            <KV k="Next 7 days" v={d.totals.week_text} />) : null}
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
                        && hint.variables?.payee === b.payee)
                    || offline}
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
                    accessibilityRole="button" accessibilityLabel="Dismiss"
                    onPress={() => setUndoAct(null)}>✕</Text>
            </View>
          )}
        </Card>
      )}
      {notice && (
        <Note style={[s.mut, { textAlign: "center" }]}>{notice}</Note>
      )}
      {/* income first, then the proposed changes, then the bills — the
          web page's order: money in before money out.
          A row opens the payee's HISTORY (the web's name link); the
          editor lives one tap further, like the web's ✎ expander. */}
      {income.length > 0 && (
        <Card>
          <H>Income</H>
          {income.map((b) => (
            <BillRow key={b.payee} b={b} onHint={onHint} past={isPast}
              onPress={() => router.push({ pathname: "/bill-history",
                params: { payee: b.payee } } as never)} />
          ))}
        </Card>
      )}
      {d && d.proposals.length > 0 && (
        <Card>
          <View style={{ flexDirection: "row", alignItems: "center",
                         justifyContent: "space-between", gap: 8 }}>
            <H>Proposed changes</H>
            {/* one click for the common case, as on the web — the
                finder's first pass over a fresh history is usually right */}
            {!viewer && d.proposals.length > 1 && (
              <Pressable accessibilityRole="button" style={[s.pBtn, offlineGrey(offline)]}
                         disabled={approveAll.isPending || act.isPending || offline}
                         onPress={() => approveAll.mutate(
                           d.proposals.map((p) => p.id))}>
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
            // this row's own decision in flight — never the whole list —
            // or Approve all, which is deciding this row too
            const busy = approveAll.isPending
              || (act.isPending && act.variables?.pid === p.id) || offline;
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
                  {/* the server's words for what approving does, in the
                      web's tones: remove is red, add is green, the rest
                      amber; an older server sends none and shows none */}
                  {p.change_text ? (
                    <Pill text={p.change_text}
                          tone={p.kind === "remove" ? "bad"
                            : p.kind === "add" ? "good" : "warn"} />
                  ) : null}
                  {p.kind === "attach" && p.bill_payee ? (
                    <Text style={{ color: C.mut, fontSize: 12 }}>
                      a fee of {p.bill_payee}</Text>) : null}
                  {p.kind === "merchant" && p.bill_payee ? (
                    <Text style={{ color: C.mut, fontSize: 12 }}>
                      add to bill “{p.bill_payee}”</Text>) : null}
                </View>
                <Text style={s.mut}>
                  {[money(p.amount), p.cadence_text,
                    p.upcoming_due ? `next ${mmddyy(p.upcoming_due)}` : null,
                    p.tag_text, p.summary].filter(Boolean).join(" · ")}
                </Text>
              </View>
              {!viewer && (<>
              <Pressable accessibilityRole="button" style={[s.pBtn, busy && { opacity: 0.5 }]}
                         disabled={busy}
                         onPress={() => act.mutate({ pid: p.id,
                                                     action: "approve" })}>
                <Text style={s.pBtnText}>Approve</Text>
              </Pressable>
              <Pressable accessibilityRole="button"
                         style={[s.pBtn, s.pBtnQuiet, busy && { opacity: 0.5 }]}
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
              {errText(act.error)}
            </Text>
          ) : null}
        </Card>
      )}
      {GROUP_ORDER.filter((g) => !g.includes("envelope")).map((g) => {
        const list = billsByGroup.get(g);
        if (!list?.length) return null;
        return (
          <Card key={g}>
            <H>{g}</H>
            {list.map((b) => (
              <BillRow key={b.payee} b={b} onHint={onHint} past={isPast}
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
              <BillRow key={b.payee} b={b} onHint={onHint} past={isPast}
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
              <BillRow key={b.payee} b={b} onHint={onHint} past={isPast}
                onPress={() => router.push({ pathname: "/bill-history",
                    params: { payee: b.payee } } as never)} />
            ))}
          </Card>
        );
      })()}
      {d && d.bills.length === 0 && d.proposals.length === 0
        && !query.isPending && (
        // the action is the button's own name, and only for those who may
        // run it (a viewer has none) — the web's line
        <EmptyState title={EMPTY_LINES.bills}
          action={!viewer && !detect.isPending && !offline
            ? EMPTY_LINES.billsAction : undefined}
          onAction={() => detect.mutate()} />
      )}
      {(d?.archived ?? []).length > 0 && (
        <Card>
          <Pressable onPress={() => setShowArch((x) => !x)}
                     {...disclosure(showArch, `Archived — ${d!.archived.length} removed `
                       + `bill${d!.archived.length === 1 ? "" : "s"} (history kept)`)}>
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
                // either write in flight holds both: a restore racing a
                // purge leaves the row in whichever state lands last
                const rowBusy = restoring || deleting || offline;
                return (<>
                <Pressable accessibilityRole="button" style={[s.pBtn, s.pBtnQuiet,
                             rowBusy && { opacity: 0.5 }]}
                           disabled={rowBusy}
                           onPress={() => restore.mutate(b.payee)}>
                  <Text style={[s.pBtnText, { color: C.mut }]}>
                    {restoring ? "restoring…" : "Restore"}
                  </Text>
                </Pressable>
                <Text style={[{ color: C.bad, fontSize: 12, padding: 4 },
                               rowBusy && { opacity: 0.5 }]}
                      onPress={() => rowBusy || Alert.alert(
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
      {(d?.bills ?? []).length > 0 && (isPast
        ? (d?.month_calendar
            ? <CalendarSection month={{ ym: shownYm, title: ymLabel(shownYm),
                                         days: d.month_calendar }} />
            : null)
        : <CalendarSection />)}

      <Sheet visible={adding} onClose={() => setAdding(false)}
             title="Add a bill">
            {/* the draft's own fields live here (keyed on `adding`) — a
                keystroke used to re-render the whole tab behind the sheet */}
            {adding && (
              <AddBillFields pending={add.isPending}
                             error={add.isError ? errText(add.error) : null}
                             archived={add.isError && archivedClash(add.error)}
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
                     category: string; isEnv: boolean; restore?: boolean };

/** The add was refused because an archived bill holds the name. */
function archivedClash(e: unknown): boolean {
  return e instanceof ApiError && e.status === 409
    && (e.body as { archived?: unknown } | undefined)?.archived === true;
}

function AddBillFields({ pending: sending, error, archived, onAdd, onCancel }: {
  pending: boolean; error: string | null; archived: boolean;
  onAdd: (f: AddBillForm) => void; onCancel: () => void;
}) {
  const { client } = useSession();
  // offline, Add and Restore are greyed with "You're offline." beside them
  const offline = useOffline();
  const pending = sending || offline;
  const [nPayee, setNPayee] = useState("");
  const [nAmount, setNAmount] = useState("");
  const [nCadence, setNCadence] = useState("MONTHLY:1");
  const [nCustomN, setNCustomN] = useState("2");
  const [nCustomUnit, setNCustomUnit] = useState("YEARLY");
  const [nDue, setNDue] = useState("");
  const [nIncome, setNIncome] = useState(false);
  const [nPinned, setNPinned] = useState(false);
  const [nCategory, setNCategory] = useState("");
  // known ledger merchants — picking one makes the bill match its past
  // and future transactions (the web datalist)
  const merchants = useQuery({ queryKey: ["merchants"],
    queryFn: () => client!.merchants(), enabled: !!client });
  const effCadence = nCadence === "CUSTOM"
    ? customCadence(nCustomUnit, nCustomN) : nCadence;
  const nIsEnv = effCadence.startsWith("ENVELOPE");
  const form = (): AddBillForm => ({ payee: nPayee, amount: nAmount,
    cadence: effCadence, due: nDue, income: nIncome, pinned: nPinned,
    category: nCategory, isEnv: nIsEnv });
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
      <TextInput {...fieldA11y("Merchant — pick a known one or type a new name")} style={s.input}
                 placeholder="merchant — pick a known one or type a new name"
                 placeholderTextColor={C.mut} value={nPayee}
                 onChangeText={setNPayee} />
      {payeeSuggestions.length > 0 && (
        <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
          {payeeSuggestions.map((m) => (
            <Pressable accessibilityRole="button"
                       key={m} style={s.chip} onPress={() => setNPayee(m)}>
              <Text style={{ color: C.accent, fontSize: 12 }}>{m}</Text>
            </Pressable>
          ))}
        </View>
      )}
      <TextInput {...fieldA11y("Amount")} style={s.input} placeholder="amount"
                 placeholderTextColor={C.mut} value={nAmount}
                 onChangeText={setNAmount} keyboardType="decimal-pad" />
      <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6 }}>
        {/* FREQ:interval values, the same wire format the web
            add-form sends — never the human label. The list is the
            web's add list (ADD_CADENCES, checked against the web copy)
            + Custom: no invented options. */}
        {([...ADD_CADENCES, ["CUSTOM", "custom interval…"],
          ] as [string, string][]).map(([v, l]) => (
          <Pressable accessibilityRole="button" key={v}
                     style={[s.chip, nCadence === v && s.chipOn]}
                     onPress={() => setNCadence(v)}>
            <Text style={{ color: nCadence === v ? C.text : C.mut,
                           fontSize: 12 }}>{l}</Text>
          </Pressable>
        ))}
      </View>
      {nCadence === "CUSTOM" && (
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 6,
                       flexWrap: "wrap" }}>
          <Text style={{ color: C.mut, fontSize: 12 }}>every</Text>
          {/* the cap depends on the unit (about 20 years of it), so the
              field clamps as it is typed rather than at a fixed length */}
          <TextInput {...fieldA11y("Repeats every")} style={[s.input, { width: 60 }]}
                     keyboardType="number-pad" value={nCustomN} maxLength={4}
                     onChangeText={(t) => setNCustomN(
                       Number(t) > customMax(nCustomUnit)
                         ? String(customMax(nCustomUnit)) : t)} />
          {CUSTOM_UNITS.map(([u, label]) => (
            <Pressable accessibilityRole="button" key={u}
                       style={[s.chip, nCustomUnit === u && s.chipOn]}
                       onPress={() => setNCustomUnit(u)}>
              <Text style={{ color: nCustomUnit === u
                               ? C.text : C.mut, fontSize: 12 }}>
                {label}
              </Text>
            </Pressable>
          ))}
        </View>
      )}
      {nIsEnv && (
        <>
          <TextInput {...fieldA11y("Envelope category (optional)")} style={s.input}
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
                  <Pressable accessibilityRole="button" key={c} style={s.chip}
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
      <TextInput {...fieldA11y("Next due (YYYY-MM-DD, optional)")} style={s.input}
                 placeholder="next due (YYYY-MM-DD, optional)"
                 placeholderTextColor={C.mut} value={nDue}
                 onChangeText={setNDue} autoCapitalize="none" />
      <View style={{ flexDirection: "row", alignItems: "center",
                     gap: 8 }}>
        <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
          This is income
        </Text>
        <Switch value={nIncome}
                accessibilityLabel="This is income"
                onValueChange={setNIncome}
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
      {!nIncome && (
        <View style={{ flexDirection: "row", alignItems: "center",
                       gap: 8 }}>
          <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
            Pin to Today (its own card + daily email)
          </Text>
          <Switch value={nPinned}
                  accessibilityLabel="Pin to Today"
                  onValueChange={setNPinned}
                  trackColor={{ true: C.accent, false: C.hover }}
                  thumbColor={C.text} />
        </View>
      )}
      {error ? (
        <Note bad style={{ color: C.bad, fontSize: 12 }}>{error}</Note>
      ) : null}
      <View style={{ flexDirection: "row", gap: 8,
                     justifyContent: "center" }}>
        {/* an archived bill holds this name: bring it back with the
            values entered here */}
        {archived && (
          <Pressable accessibilityRole="button" style={[s.pBtn, pending && { opacity: 0.5 }]}
                     disabled={pending}
                     onPress={() => onAdd({ ...form(), restore: true })}>
            <Text style={s.pBtnText}>Restore</Text>
          </Pressable>
        )}
        <Pressable accessibilityRole="button" style={[s.pBtn,
                     (!nPayee.trim() || !nAmount || pending)
                       && { opacity: 0.5 }]}
                   disabled={!nPayee.trim() || !nAmount || pending}
                   onPress={() => onAdd(form())}>
          <Text style={s.pBtnText}>Add</Text>
        </Pressable>
        <Pressable accessibilityRole="button" style={[s.pBtn, s.pBtnQuiet]} onPress={onCancel}>
          <Text style={[s.pBtnText, { color: C.mut }]}>Cancel</Text>
        </Pressable>
      </View>
      <OfflineNote />
    </>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  bill: { borderTopColor: C.border, borderTopWidth: StyleSheet.hairlineWidth,
          paddingVertical: 8 },
  // the row's press target, under the row's content
  billPress: { position: "absolute", top: 0, right: 0, bottom: 0, left: 0 },
  billTop: { flexDirection: "row", justifyContent: "space-between", gap: 8 },
  // the status and its pills, which pass a press through to the row
  passRow: { flexDirection: "row", alignItems: "center", gap: 6 },
  billSub: { flexDirection: "row", justifyContent: "space-between",
             alignItems: "center", gap: 8, marginTop: 2 },
  payee: { color: C.text, fontSize: 14, fontWeight: "600", flexShrink: 1 },
  amount: { color: C.text, fontSize: 14, fontVariant: ["tabular-nums"] },
  mut: { color: C.mut, fontSize: 12 },
  proposal: { flexDirection: "row", alignItems: "center", gap: 8,
              borderTopColor: C.border,
              borderTopWidth: StyleSheet.hairlineWidth,
              paddingVertical: 8 },
  pBtn: { backgroundColor: C.btn, borderRadius: 8,
          paddingHorizontal: 12, paddingVertical: 7 },
  pBtnQuiet: { backgroundColor: C.hover },
  pBtnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  toolRow: { flexDirection: "row", gap: 8, marginHorizontal: 12,
             marginTop: 10 },
  tool: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 14, paddingVertical: 7,
          minHeight: 44, minWidth: 44, alignItems: "center",
          justifyContent: "center" },
  toolText: { color: C.accent, fontSize: 13, fontWeight: "600" },
  input: { backgroundColor: C.bg, borderColor: C.field, borderWidth: 1,
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

// A server older than the queue's words: the story built from the fields
// the row carries, as that server's web built it.
function olderAttentionWords(b: Bill): string {
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
  return (
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
      : "payments match, but the due-date anchor is off the real draft day");
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
  // the story is the server's (attention_text, api_bills.attention_words)
  const story = b.attention_text === undefined
    // older-server path: stands in for `attention_text`
    ? olderAttentionWords(b) : (b.attention_text ?? "");
  const btn = (label: string, onPress: () => void, primary = false) => (
    <Pressable accessibilityRole="button" onPress={onPress} disabled={busy}
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
      <Pressable accessibilityRole="button" onPress={onReview}
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
