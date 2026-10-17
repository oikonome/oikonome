// The wizard's "Bills & budgets" step, in its three panes — the web's:
// the question (find them for me / I'll enter them myself), then either
// the finder's proposals with an Adjust form on each, or a hand-entry
// form with the bills added so far. Both write through the same doors the
// Bills tab uses, so nothing here is a wizard-only kind of bill.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Pressable, Text, TextInput, View } from "react-native";

import { errText } from "../lib/api";
import type { BillsData, Proposal } from "../lib/api";
import { removeFromList } from "../lib/cache";
import { cadenceChoices, cadenceWords, localYmd, money,
         proposalSuperseded } from "../lib/pure";
import { useSession } from "../lib/session";
import { C, mmddyy } from "../lib/theme";
import { useOwner, useViewer } from "../lib/viewer";
import Select from "./select";
import { Pill } from "./ui";
import { adjustOf, type AdjustForm, billLine, cadenceTakesDueDay, dueDayOf,
         MANUAL_CADENCES, manualTally, nextDueForDay, parseDueDay,
         proposalAdjustable, proposalCadenceValue } from "./wizard-logic";
import { OptionCard, PillRow, TAP, ws } from "./wizard-ui";

export type SetupMode = "auto" | "manual";

/** The question pane's two answers, the web's words. The answer is the
 *  owner's (the server refuses anyone else's with a 403), so everyone
 *  else reads the household's answer instead of cards that snap back. */
export function BillsQuestion({ value, onPick, disabled }: {
  value: SetupMode; onPick: (v: SetupMode) => void; disabled?: boolean;
}) {
  const owner = useOwner();
  if (!owner)
    return (
      <View style={ws.body}>
        <Text style={{ color: C.text, fontSize: 14 }}>
          {value === "manual" ? "Bills are entered by hand"
            : "The finder proposes bills and paychecks"}</Text>
        <Text style={ws.mut}>the household owner sets this</Text>
      </View>
    );
  return (
    <View style={ws.body} accessibilityRole="radiogroup">
      <OptionCard title="Find them for me, I'll adjust" tag="RECOMMENDED"
        caption={"The finder proposes every bill and paycheck it can prove "
          + "from your ledger and pre-fills the budget from real months. "
          + "Nothing counts until you approve it; edit or dismiss anything."}
        selected={value === "auto"} disabled={disabled}
        onPress={() => onPick("auto")} />
      <OptionCard title="I'll enter them myself"
        caption={"Start with an empty bills list and blank budgets. "
          + "Detection stays available later from Bills → \"Find bills & "
          + "income\" and still never adds a bill without your approval."}
        selected={value === "manual"} disabled={disabled}
        onPress={() => onPick("manual")} />
    </View>
  );
}

// a decided or added bill moves the counts, the budget prefill (which
// reads pending income proposals) and, when a bill was written, the
// calendar and Today. ["bills"] is this step's own list and the wizard is
// a pushed screen with no shown-refetch of its own, so it refetches now;
// the screens behind it refresh when next shown.
function useBillsRefetch() {
  const qc = useQueryClient();
  return (wroteBill: boolean) => {
    qc.invalidateQueries({ queryKey: ["bills"] });
    qc.invalidateQueries({ queryKey: ["onboarding"] });
    qc.invalidateQueries({ queryKey: ["budgets-suggest"] });
    if (!wroteBill) return;
    qc.invalidateQueries({ queryKey: ["calendar"], refetchType: "none" });
    qc.invalidateQueries({ queryKey: ["today"], refetchType: "none" });
  };
}

/** The automatic path: every pending proposal with Dismiss / Approve
 *  (and Adjust on an offer to add one), and Approve all above them, right in the step. Once nothing
 *  is pending it says what is tracked. */
export function ProposalsInline() {
  const { client } = useSession();
  const qc = useQueryClient();
  const viewer = useViewer();
  const refetch = useBillsRefetch();
  const q = useQuery({ queryKey: ["bills"],
    queryFn: () => client!.bills(), enabled: !!client,
    // mounted right after the scan: never trust a cached list here
    refetchOnMount: "always" });
  const [notice, setNotice] = useState<string | null>(null);
  const [approved, setApproved] = useState(0);
  const [adjusting, setAdjusting] = useState<string | null>(null);
  // a refused "Save & approve" (a name already taken, charges another
  // bill already tracks) is answered inside the open form, which keeps
  // what the person typed so they can change the one field at fault
  const [adjustErr, setAdjustErr] = useState<{ pid: string; text: string }
    | null>(null);
  const [byHand, setByHand] = useState(false);
  const act = useMutation({
    mutationFn: ({ pid, action, adjust }: {
        pid: string; action: "approve" | "reject";
        adjust?: Parameters<NonNullable<typeof client>["billsProposal"]>[2] }) =>
      client!.billsProposal(pid, action, adjust),
    onSuccess: (r, v) => {
      const gone = proposalSuperseded(r);
      setNotice(gone ? `${r.payee} is already tracked — the offer was withdrawn`
        : `${v.action === "approve" ? "Approved" : "Dismissed"}: ${r.payee}`
          // the approve changed something the person set (a pin cleared)
          + (r.note ? ` — ${r.note}` : ""));
      if (v.action === "approve" && !gone) setApproved((n) => n + 1);
      if (adjusting === v.pid) setAdjusting(null);
      // the server has committed it: the card leaves now
      removeFromList<BillsData, Proposal>(qc, ["bills"], "proposals",
        (p) => p.id === v.pid);
      refetch(v.action === "approve" && !gone);
    },
    onError: (e, v) => {
      if (v.adjust && !proposalSuperseded(e) && adjusting === v.pid) {
        setAdjustErr({ pid: v.pid, text: errText(e) });
        return;
      }
      setNotice(errText(e));
      // an offer for a bill already tracked is withdrawn by the server as
      // it refuses: the card could neither be approved nor dismissed
      if (proposalSuperseded(e)) {
        removeFromList<BillsData, Proposal>(qc, ["bills"], "proposals",
          (p) => p.id === v.pid);
        refetch(false);
      }
    },
  });
  const approveAll = useMutation({
    mutationFn: () => client!.billsApproveAll(),
    onSuccess: (r) => {
      setNotice((r.failed.length
        ? `Approved ${r.approved}; ${r.failed.length} stayed pending.`
        : `Approved all ${r.approved} proposal${r.approved === 1 ? "" : "s"}.`)
        + (r.notes?.length
          ? ` — ${r.notes.map((n) => `${n.payee}: ${n.note}`).join("; ")}` : ""));
      setApproved((n) => n + r.approved);
      setAdjusting(null);
      // only what stayed pending is still a proposal
      qc.setQueryData<BillsData>(["bills"], (old) => old && {
        ...old,
        proposals: old.proposals.filter(
          (p) => r.failed.some((f) => f.pid === p.id)),
      });
      refetch(true);
    },
    onError: (e) => setNotice(errText(e)),
  });
  if (!q.data) return <Text style={ws.mut}>Loading proposals…</Text>;
  const props = q.data.proposals;
  const tracked = q.data.bills.length;
  const busyAny = act.isPending || approveAll.isPending;
  return (
    <View style={{ marginTop: 10 }}>
      {notice ? <Text style={[ws.mut, { color: C.good }]}>{notice}</Text> : null}
      {props.length === 0 ? (
        <Text style={ws.mut}>
          {tracked > 0
            ? `${tracked} recurring bill${tracked === 1 ? "" : "s"} & `
              + "paychecks tracked — nothing left to review."
            : "No proposals — the finder found no recurring pattern yet. "
              + "Continue; it runs again as history grows."}
        </Text>
      ) : (
        <>
          {!viewer && props.length > 1 && (
            <Pressable style={[ws.btn, { alignSelf: "flex-start",
                                         marginTop: 6 },
                               busyAny && { opacity: 0.5 }]}
                       disabled={busyAny}
                       onPress={() => approveAll.mutate()}>
              <Text style={ws.btnText}>
                {approveAll.isPending ? "Approving…"
                  : `Approve all ${props.length}`}
              </Text>
            </Pressable>
          )}
          {props.map((x) => {
            // this row's own decision in flight — never the whole list
            const busy = act.isPending && act.variables?.pid === x.id;
            const open = adjusting === x.id;
            return (
              <View key={x.id}
                    style={[ws.row, { flexDirection: "column",
                                      alignItems: "stretch", gap: 6 },
                            open && { borderLeftWidth: 3,
                                      borderLeftColor: C.accent,
                                      paddingLeft: 8 }]}>
                <View>
                  <View style={{ flexDirection: "row", alignItems: "center",
                                 gap: 6, flexWrap: "wrap" }}>
                    <Text style={{ color: C.text, fontSize: 14,
                                   fontWeight: "600", flexShrink: 1 }}
                          numberOfLines={1}>{x.payee}</Text>
                    {x.kind ? (
                      <Pill text={x.kind}
                            tone={x.kind === "remove" ? "bad"
                              : x.kind === "add" ? "good" : "warn"} />
                    ) : null}
                    {x.income ? <Pill text="income" tone="good" /> : null}
                    {x.bill_type === "envelope" ? <Pill text="envelope" /> : null}
                  </View>
                  <Text style={ws.mut}>
                    {x.cadence} · {x.income ? "+" : ""}{money(x.amount)}
                    {x.next_due ? ` · next ${mmddyy(x.next_due)}` : ""}
                    {x.summary ? ` · ${x.summary}` : ""}
                    {open ? " — " : ""}
                    {open ? <Text style={{ color: C.accent }}>adjusting</Text>
                          : null}
                  </Text>
                </View>
                {open && proposalAdjustable(x) ? (
                  <AdjustFields p={x} cadences={q.data!.cadences ?? []}
                    busy={busy}
                    refused={adjustErr?.pid === x.id ? adjustErr.text : null}
                    onCancel={() => { setAdjusting(null); setAdjustErr(null); }}
                    onSave={(adjust) => {
                      setAdjustErr(null);
                      act.mutate({ pid: x.id, action: "approve", adjust });
                    }} />
                ) : !viewer && (
                  <View style={{ flexDirection: "row", gap: 8,
                                 flexWrap: "wrap" }}>
                    <Pressable style={[ws.btn, ws.quiet,
                                       busy && { opacity: 0.5 }]}
                               disabled={busy}
                               onPress={() => act.mutate({ pid: x.id,
                                                           action: "reject" })}>
                      <Text style={[ws.btnText, { color: C.mut }]}>Dismiss</Text>
                    </Pressable>
                    {/* only a new bill or paycheck has fields to adjust; a
                        fee, merchant, removal or change offer edits an
                        existing bill, and the server refuses an adjust */}
                    {proposalAdjustable(x) && (
                      <Pressable style={[ws.btn, ws.quiet,
                                         busy && { opacity: 0.5 }]}
                                 disabled={busy}
                                 onPress={() => {
                                   setAdjusting(x.id); setAdjustErr(null);
                                 }}>
                        <Text style={ws.btnText}>Adjust</Text>
                      </Pressable>
                    )}
                    <Pressable style={[ws.btn, busy && { opacity: 0.5 }]}
                               disabled={busy}
                               onPress={() => act.mutate({ pid: x.id,
                                                           action: "approve" })}>
                      <Text style={ws.btnText}>Approve</Text>
                    </Pressable>
                  </View>
                )}
              </View>
            );
          })}
          <Text style={[ws.mut, { fontSize: 13 }]}>
            {props.length} proposal{props.length === 1 ? "" : "s"} ·{" "}
            {approved} approved. Adjust changes the name, amount, cadence
            or due day before it is approved; the finder's evidence stays
            attached.
          </Text>
        </>
      )}
      {!viewer && (byHand ? (
        <View style={ws.section}>
          <AddBillForm />
        </View>
      ) : (
        <Pressable onPress={() => setByHand(true)}
                   style={{ minHeight: TAP, justifyContent: "center" }}>
          <Text style={{ color: C.accent, fontSize: 13 }}>
            Add a bill by hand</Text>
        </Pressable>
      ))}
    </View>
  );
}

/** One proposal's Adjust form: name, amount, cadence, due day; Cancel or
 *  "Save & approve". Only what changed rides with the approve. */
function AdjustFields({ p, cadences, busy, refused, onCancel, onSave }: {
  p: Proposal; cadences: [string, string][]; busy: boolean;
  // the server's refusal of the last save, shown where the form's own
  // checks show theirs
  refused?: string | null;
  onCancel: () => void;
  onSave: (adjust: Exclude<ReturnType<typeof adjustOf>,
                           { error: string }>["adjust"]) => void;
}) {
  const own = proposalCadenceValue(p);
  const [f, setF] = useState<AdjustForm>({
    name: p.payee, amount: Math.abs(p.amount).toFixed(2),
    cadence: own ?? "",
    dueDay: String(dueDayOf(p.next_due) ?? "") });
  const [err, setErr] = useState<string | null>(null);
  // the server's list plus the proposal's own cadence when it is not on
  // it — touching the picker must never lose the cadence that was found
  const options = cadenceChoices(cadences, [own])
    .map(([value, label]) => ({ value, label }));
  const set = (patch: Partial<AdjustForm>) => {
    setErr(null); setF((o) => ({ ...o, ...patch }));
  };
  return (
    <View style={{ gap: 8 }}>
      <TextInput style={ws.input} value={f.name} placeholder="Name"
                 placeholderTextColor={C.mut} accessibilityLabel="Name"
                 onChangeText={(t) => set({ name: t })} />
      <TextInput style={ws.input} value={f.amount} placeholder="Amount"
                 placeholderTextColor={C.mut} accessibilityLabel="Amount"
                 keyboardType="decimal-pad"
                 onChangeText={(t) => set({ amount: t })} />
      <Select title="Cadence"
              placeholder={own ? cadenceWords(own) : "cadence"}
              value={f.cadence} options={options}
              onChange={(v) => set({ cadence: v || (own ?? "") })} />
      {/* a day of the month anchors only a monthly or yearly bill;
          adjustOf drops a typed day the field no longer shows */}
      {cadenceTakesDueDay(f.cadence || own) && (
        <TextInput style={ws.input} value={f.dueDay}
                   placeholder="Due day (1–31)" placeholderTextColor={C.mut}
                   accessibilityLabel="Due day of the month"
                   keyboardType="number-pad" maxLength={2}
                   onChangeText={(t) => set({ dueDay: t })} />
      )}
      {err || refused ? (
        <Text style={[ws.mut, { color: C.warn }]}>{err || refused}</Text>
      ) : null}
      <View style={{ flexDirection: "row", gap: 8, justifyContent: "flex-end",
                     flexWrap: "wrap" }}>
        <Pressable style={[ws.btn, ws.quiet]} onPress={onCancel}>
          <Text style={ws.btnText}>Cancel</Text>
        </Pressable>
        <Pressable style={[ws.btn, busy && { opacity: 0.5 }]} disabled={busy}
                   onPress={() => {
                     const r = adjustOf(p, f);
                     if ("error" in r) { setErr(r.error); return; }
                     onSave(r.adjust);
                   }}>
          <Text style={ws.btnText}>
            {busy ? "Saving…" : "Save & approve"}</Text>
        </Pressable>
      </View>
    </View>
  );
}

/** The hand-entry form: name, cadence, amount, due day; Add bill or Add
 *  paycheck through the bill-save door the Bills tab's add form uses. */
function AddBillForm() {
  const { client } = useSession();
  const refetch = useBillsRefetch();
  const [name, setName] = useState("");
  const [cadence, setCadence] = useState("MONTHLY:1");
  const [amount, setAmount] = useState("");
  const [due, setDue] = useState("");
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const add = useMutation({
    mutationFn: (income: boolean) => {
      const day = parseDueDay(due);
      return client!.billsSave({
        payee: name.trim(), amount: amount.trim(), cadence, income,
        ...(day ? { next_due: nextDueForDay(day, localYmd(new Date())) }
                : {}) });
    },
    onSuccess: (r, income) => {
      setMsg({ text: `${r?.restored ? "Restored" : "Added"} ${name.trim()}`
        + (income ? " (paycheck)." : ".") });
      setName(""); setAmount(""); setDue("");
      refetch(true);
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  const submit = (income: boolean) => {
    if (!name.trim()) { setMsg({ text: "Give it a name.", bad: true }); return; }
    if (!(Number(amount.replace(/[$,\s]/g, "")) > 0)) {
      setMsg({ text: "Enter an amount above zero.", bad: true }); return;
    }
    if (due.trim() && parseDueDay(due) === null) {
      setMsg({ text: "Due day is a day of the month, 1–31.", bad: true });
      return;
    }
    setMsg(null);
    add.mutate(income);
  };
  return (
    <View style={{ gap: 8 }}>
      <TextInput style={ws.input} value={name}
                 placeholder="Bill name (e.g. Rent)"
                 placeholderTextColor={C.mut} onChangeText={setName} />
      <PillRow choices={MANUAL_CADENCES} value={cadence} onPick={setCadence}
               label="Cadence" />
      <TextInput style={ws.input} value={amount} placeholder="Amount"
                 placeholderTextColor={C.mut} keyboardType="decimal-pad"
                 onChangeText={setAmount} />
      <TextInput style={ws.input} value={due}
                 placeholder="Due day (1–31)" placeholderTextColor={C.mut}
                 accessibilityLabel="Due day of the month"
                 keyboardType="number-pad" maxLength={2}
                 onChangeText={setDue} />
      <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap" }}>
        <Pressable style={[ws.btn, ws.quiet, add.isPending && { opacity: 0.5 }]}
                   disabled={add.isPending} onPress={() => submit(false)}>
          <Text style={ws.btnText}>+ Add bill</Text>
        </Pressable>
        <Pressable style={[ws.btn, ws.quiet, add.isPending && { opacity: 0.5 }]}
                   disabled={add.isPending} onPress={() => submit(true)}>
          <Text style={ws.btnText}>+ Add paycheck</Text>
        </Pressable>
      </View>
      {msg ? <Text style={[ws.mut, { color: msg.bad ? C.warn : C.good }]}>
        {msg.text}</Text> : null}
    </View>
  );
}

/** The manual path: the add form, then every bill and paycheck the
 *  household has, each marked added. */
export function ManualBills() {
  const { client } = useSession();
  const viewer = useViewer();
  const q = useQuery({ queryKey: ["bills"],
    queryFn: () => client!.bills(), enabled: !!client,
    refetchOnMount: "always" });
  const list = q.data?.bills ?? [];
  const income = list.filter((b) => b.income).length;
  return (
    <View style={ws.body}>
      {!viewer && <AddBillForm />}
      {list.map((b) => (
        <View key={b.payee} style={[ws.row, { justifyContent: "space-between" }]}>
          <View style={{ flex: 1 }}>
            <Text style={{ color: C.text, fontSize: 14, fontWeight: "600" }}
                  numberOfLines={1}>{b.payee}</Text>
            <Text style={ws.small}>
              {billLine(b.amount, b.cadence_value ?? b.cadence, b.due_on)}</Text>
          </View>
          <Text style={[ws.chip, { color: C.good, borderColor: C.good }]}>
            {b.income ? "income" : "added"}</Text>
        </View>
      ))}
      <Text style={[ws.mut, { fontSize: 13 }]}>
        {manualTally(list.length - income, income)} Nothing here was
        detected; the finder is off for this household until you turn it on.
      </Text>
    </View>
  );
}
