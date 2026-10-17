// The wizard's "Email & push" step, on every instance: how the daily
// verdict reaches the household (email, push, both, off), when, in which
// form, push to this phone, and who gets the email. A self-hosted
// instance adds one line about its own outbound mail account; a hosted
// one sends through the operator's and never mentions it.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "expo-router";
import { useState } from "react";
import { Pressable, Switch, Text, TextInput, View } from "react-native";

import { errText } from "../lib/api";
import { SETTINGS_SAVES, seedSettings } from "../lib/cache";
import { type Delivery, deliveryOf, flagsOf, looksLikeMailbox,
         NEED_BASE_URL, recipientDuplicate, recipientEditBody,
         SHARED_GRANT_NOTE } from "../lib/pure";
import { registerNativePush } from "../lib/push";
import { rebaseSched, storedSched } from "../lib/schedule";
import { useSession } from "../lib/session";
import { C } from "../lib/theme";
import { useSharedLock } from "../lib/viewer";
import { VerifyEmailCard } from "./verify-email-card";
import { clockHour, recipientRole } from "./wizard-logic";
import { PillRow, TAP, ws } from "./wizard-ui";

export type RecipRow = { email: string; owner: boolean; muted?: boolean;
                         via?: string; invite?: string | null };

/** The rows "Who gets the email" lists: the owner first (always on),
 *  then every member and added address with its own on/off. An older
 *  server without the status list still shows the saved addresses. */
export function recipientRows(sv: Record<string, unknown> | undefined,
                              ownerEmail: string | undefined): RecipRow[] {
  const status = (sv?.email_recipient_status as RecipRow[] | null) ?? [];
  if (status.length)
    return [...status.filter((r) => r.owner),
            ...status.filter((r) => !r.owner)];
  const muted = new Set(((sv?.email_muted as string[] | null) ?? [])
    .map((x) => x.toLowerCase()));
  return [
    ...(ownerEmail ? [{ email: ownerEmail, owner: true }] : []),
    ...((sv?.email_recipients as string[] | null) ?? []).map((e) => ({
      email: e, owner: false, muted: muted.has(e.toLowerCase()) })),
  ];
}

type DailyFlags = { on: boolean; push: boolean; hour: number;
                    summary: boolean };

export function EmailPane({ hosted }: { hosted: boolean }) {
  const { client } = useSession();
  const router = useRouter();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
                        enabled: !!client });
  const notify = useQuery({ queryKey: ["notify"],
    queryFn: () => client!.notifyStatus(), enabled: !!client });
  const sv = settings.data as Record<string, unknown> | undefined;
  const daily = (sv?.email_schedule as { daily?: Partial<DailyFlags> }
                 | null | undefined)?.daily;
  // nothing to lay a change over until the stored settings have answered
  const notLoaded = !settings.data;
  // delivery = the email flag (`on`) and the push flag, read as one
  // choice; with no schedule saved the daily email is ON by default. A
  // local override holds the tap's answer while the write lands.
  const [deliveryOv, setDeliveryOv] = useState<Delivery | null>(null);
  const delivery = deliveryOv ?? deliveryOf(daily?.on ?? true, !!daily?.push);
  const { on, push } = flagsOf(delivery);
  const [hour, setHour] = useState<number | null>(null);
  const [summary, setSummary] = useState<boolean | null>(null);
  const hourVal = hour ?? daily?.hour ?? 7;
  const summaryVal = summary ?? daily?.summary ?? false;
  // a phone receives push only through the mobile-app relay — browser
  // push reaches browsers, never this app, so it does not make the
  // choice available here; registering would "succeed" and deliver nothing
  const pushReady = !!notify.data?.native_push_available;
  const [pushNote, setPushNote] = useState<{ text: string; bad?: boolean }
                                           | null>(null);
  const [pushBusy, setPushBusy] = useState(false);
  // The schedule saves whole, so the daily change is laid over the
  // schedule stored NOW (the Email & Push screen's rule): the other
  // reports, and any daily field this tap did not change, stay as they
  // are stored. One settings save at a time, so each read sees the one
  // before it; the reply seeds ["settings"].
  const save = useMutation({
    mutationFn: async (v: { base: DailyFlags; patch: Partial<DailyFlags> }) => {
      const live = (await client!.settingsSchedule()) ?? {};
      const sent = rebaseSched({ daily: { ...v.base, ...v.patch } },
                               { daily: v.base }, live, "daily");
      const r = await client!.settingsSaveSchedule(sent);
      seedSettings(qc, { ...r, email_schedule: storedSched(r, sent) });
      return r;
    },
    scope: SETTINGS_SAVES,
    onError: () => setDeliveryOv(null),
  });
  const commit = (patch: Partial<DailyFlags>) =>
    save.mutate({ base: { on: daily?.on ?? true, push: !!daily?.push,
                          hour: daily?.hour ?? 7,
                          summary: daily?.summary ?? false },
                  patch: { on, push, hour: hourVal, summary: summaryVal,
                           ...patch } });
  // registering THIS phone — the same door the launch offers once — and
  // saying what the OS answered, so the choice is not a promise the
  // phone never agreed to
  const turnOnPush = async () => {
    if (!client) return;
    setPushBusy(true); setPushNote(null);
    try {
      const r = await registerNativePush(client, { force: true });
      setPushNote(
        r === "ok" ? { text: "This phone will receive it." }
        : r === "shared" ? { bad: true, text: SHARED_GRANT_NOTE }
        : r === "denied" ? { bad: true, text: "Notifications are off for "
            + "this app in the phone's settings — allow them, then tap "
            + "Turn on again." }
        : r === "unsupported" ? { bad: true, text: "This build cannot "
            + "receive push; the installed app can." }
        : { bad: true, text: "Could not register this phone for push." });
      if (r === "ok") qc.invalidateQueries({ queryKey: ["notify"] });
    } finally { setPushBusy(false); }
  };
  const choose = (next: Delivery) => {
    setDeliveryOv(next);
    commit(flagsOf(next));
    if (next === "push" || next === "both") turnOnPush();
  };

  // ---- who gets the email ----
  const lockAdd = useSharedLock("grant");
  const needBaseUrl = !!notify.data?.invites_need_base_url;
  const rows = recipientRows(sv, me.data?.email);
  const recipients = (sv?.email_recipients as string[] | null) ?? [];
  const mutedList = ((sv?.email_muted as string[] | null) ?? [])
    .map((x) => x.toLowerCase());
  const [draft, setDraft] = useState("");
  const [recipErr, setRecipErr] = useState<string | null>(null);
  const recip = useMutation({
    mutationFn: (body: Record<string, unknown>) => client!.settingsSave(body),
    scope: SETTINGS_SAVES,
    onSuccess: (v) => { setRecipErr(null); seedSettings(qc, v); },
    onError: (e) => {
      setRecipErr(errText(e));
      qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
  const add = () => {
    const v = draft.trim();
    if (needBaseUrl) { setRecipErr(NEED_BASE_URL); return; }
    if (!looksLikeMailbox(v)) {
      setRecipErr("That doesn't look like an email address."); return;
    }
    const dupe = recipientDuplicate(v, recipients,
      rows.find((r) => r.owner)?.email);
    if (dupe) { setRecipErr(dupe); setDraft(""); return; }
    setRecipErr(null); setDraft("");
    recip.mutate(recipientEditBody({ kind: "add", email: v, recipients }));
  };
  const smtpSet = !!(sv?.smtp_host || sv?.smtp_password_set);

  return (
    <View style={ws.body}>
      <Text style={ws.h2}>Daily verdict</Text>
      <PillRow<Delivery> label="Daily verdict" value={delivery}
        choices={[["email", "Email"], ["push", "Push"], ["both", "Both"],
                  ["off", "Off"]]}
        disabled={(v) => notLoaded || save.isPending
          || ((v === "push" || v === "both") && !pushReady)}
        onPick={choose} />
      {/* hosted mail goes to confirmed addresses only */}
      {on && (
        <VerifyEmailCard me={me.data}
          note="Email delivery starts once your address is confirmed." />)}
      {(on || push) && (
        <View style={[ws.row, { justifyContent: "space-between" }]}>
          <Text style={{ color: C.text, fontSize: 14 }}>Send at</Text>
          <View style={{ flexDirection: "row", alignItems: "center" }}>
            <Pressable disabled={notLoaded} accessibilityLabel="An hour earlier"
                       style={{ minWidth: TAP, minHeight: TAP,
                                alignItems: "center", justifyContent: "center" }}
                       onPress={() => { const h = (hourVal + 23) % 24;
                                        setHour(h); commit({ hour: h }); }}>
              <Text style={{ color: C.accent, fontSize: 18 }}>−</Text>
            </Pressable>
            <Text style={{ color: C.text, fontSize: 14, minWidth: 70,
                           textAlign: "center" }}>{clockHour(hourVal)}</Text>
            <Pressable disabled={notLoaded} accessibilityLabel="An hour later"
                       style={{ minWidth: TAP, minHeight: TAP,
                                alignItems: "center", justifyContent: "center" }}
                       onPress={() => { const h = (hourVal + 1) % 24;
                                        setHour(h); commit({ hour: h }); }}>
              <Text style={{ color: C.accent, fontSize: 18 }}>+</Text>
            </Pressable>
          </View>
        </View>
      )}
      {on && (
        <View style={[ws.row, { justifyContent: "space-between",
                                minHeight: TAP }]}>
          <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
            Short summary instead of the full report</Text>
          <Switch value={summaryVal} disabled={notLoaded}
                  onValueChange={(v) => { setSummary(v);
                                          commit({ summary: v }); }}
                  trackColor={{ true: C.accent, false: C.border }} />
        </View>
      )}
      <View style={[ws.row, { justifyContent: "space-between" }]}>
        <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
          Push to this phone</Text>
        <Pressable style={[ws.btn, ws.quiet,
                           (!pushReady || pushBusy) && { opacity: 0.5 }]}
                   disabled={!pushReady || pushBusy} onPress={turnOnPush}>
          <Text style={ws.btnText}>{pushBusy ? "Asking…" : "Turn on"}</Text>
        </Pressable>
      </View>
      {pushNote && (
        <Text style={[ws.mut, pushNote.bad && { color: C.warn }]}>
          {pushNote.text}</Text>
      )}
      {save.isError && <Text style={[ws.mut, { color: C.warn }]}>
        {errText(save.error)}</Text>}

      <View style={ws.section}>
        <Text style={ws.h2}>Who gets the email</Text>
        {rows.map((r) => {
          const muted = r.muted
            ?? mutedList.includes(r.email.toLowerCase());
          const role = recipientRole(r);
          return (
            <View key={r.email} style={[ws.row, { justifyContent: "space-between" }]}>
              <Text style={{ color: C.text, fontSize: 13, flex: 1 }}
                    numberOfLines={1}>
                {r.email}<Text style={{ color: C.mut }}> · {role}</Text>
              </Text>
              {r.owner ? (
                <Text style={[ws.chip, { color: C.good, borderColor: C.good }]}>
                  on</Text>
              ) : (
                <Pressable disabled={recip.isPending}
                           accessibilityRole="switch"
                           accessibilityState={{ checked: !muted }}
                           accessibilityLabel={`Email to ${r.email}`}
                           onPress={() => recip.mutate(recipientEditBody({
                             kind: "mute", email: r.email, on: !muted,
                             muted: mutedList }))}
                           style={[ws.pill, !muted && ws.pillOn]}>
                  <Text style={[ws.pillText, !muted && ws.pillTextOn]}>
                    {muted ? "off" : "on"}</Text>
                </Pressable>
              )}
            </View>
          );
        })}
        {lockAdd ? (
          <Text style={ws.mut}>{lockAdd}</Text>
        ) : (
          <View style={{ flexDirection: "row", gap: 8 }}>
            <TextInput style={[ws.input, { flex: 1, minWidth: 0 }]}
                       placeholder="Add another address"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       keyboardType="email-address" value={draft}
                       onChangeText={setDraft} onSubmitEditing={add} />
            <Pressable style={[ws.btn, ws.quiet,
                               (!draft.trim() || recip.isPending)
                                 && { opacity: 0.5 }]}
                       disabled={!draft.trim() || recip.isPending}
                       onPress={add}>
              <Text style={ws.btnText}>Add</Text>
            </Pressable>
          </View>
        )}
        {recipErr ? <Text style={[ws.mut, { color: C.bad }]}
                          onPress={() => setRecipErr(null)}>{recipErr}</Text>
                  : null}
        <Text style={ws.mut}>
          A new address gets one confirmation email first and can
          unsubscribe from any message. Members can also turn their own
          copy off in Settings.
        </Text>
      </View>

      {!hosted && (
        <Pressable onPress={() => router.push("/notifications" as never)}
                   style={{ minHeight: TAP, justifyContent: "center" }}>
          <Text style={ws.mut}>
            Outbound mail account — {smtpSet ? "set up" : "not set up yet"}
            {" · "}
            <Text style={{ color: C.accent }}>Open email settings</Text>
          </Text>
        </Pressable>
      )}
    </View>
  );
}

/** The recipients the Done step counts: every row not turned off. */
export function recipientCount(sv: Record<string, unknown> | undefined,
                               ownerEmail: string | undefined): number {
  const muted = ((sv?.email_muted as string[] | null) ?? [])
    .map((x) => x.toLowerCase());
  return recipientRows(sv, ownerEmail).filter((r) =>
    r.owner || !(r.muted ?? muted.includes(r.email.toLowerCase()))).length;
}
