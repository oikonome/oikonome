// Email & Push — the per-cadence matrix: on/off, hour, and the
// SMS/push channels, saved to the same settings the web edits.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Linking, Pressable, ScrollView, StyleSheet, Switch, Text, TextInput,
         View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import { Card, H } from "../components/ui";
import { errText, Client, EmailSchedule, Me, SettingsView } from "../lib/api";
import { patchList, patchQuery, seedSettings } from "../lib/cache";
import { useSession } from "../lib/session";
import { ext } from "../ext";
import { C } from "../lib/theme";
import { useOwner } from "../lib/viewer";
import { deviceZone, knownZones, zoneLabel } from "../lib/zone";

const CADENCES = ["daily", "weekly", "monthly", "yearly"] as const;
// the web's cadence row names — a report card is not just "Monthly"
const CADENCE_TITLE: Record<string, string> = {
  daily: "Daily verdict", weekly: "Weekly report",
  monthly: "Monthly report card", yearly: "Year in review",
};
// index = the server's weekday value (the web's WEEKDAYS list order)
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                  "Saturday", "Sunday"];

export default function Notifications() {
  const { client } = useSession();
  const qc = useQueryClient();
  // Everything on this screen writes the HOUSEHOLD's schedule, recipient
  // list and phone — owner config. Nobody else is shown it (Settings
  // hands them their own delivery switch instead), members included:
  // the recipient list is a door out of the instance, and the server
  // drops those fields from anyone else's save. A deep link lands the
  // same way.
  const viewer = !useOwner();
  const query = useQuery({
    queryKey: ["schedule"],
    queryFn: () => client!.settingsSchedule(),
    enabled: !!client,
  });
  // a channel toggle that saves but can never deliver is a lie — gate
  // SMS/push exactly like the web Settings page does
  const notify = useQuery({
    queryKey: ["notify-status"],
    queryFn: () => client!.notifyStatus(),
    enabled: !!client,
  });
  const smsOk = !!notify.data?.sms_available
    && !!notify.data?.phone_verified;
  // any channel that can deliver: web push (VAPID), a relay for the mobile
  // app (this phone can register itself), or a phone already registered
  const pushOk = !!notify.data?.push_available
    || !!notify.data?.native_push_available
    || (notify.data?.native_push_devices ?? 0) > 0;
  const [sched, setSched] = useState<EmailSchedule | null>(null);
  const [openCad, setOpenCad] = useState<string | null>(null);
  // the zone the hours are kept in — household's own, else the instance's
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
    enabled: !!client, staleTime: 5 * 60_000 });
  const household = me.data?.timezone_source === "household"
    ? (me.data.timezone ?? "") : "";
  const instanceZone = me.data?.instance_timezone ?? "";
  const effective = household || instanceZone;
  const device = deviceZone();
  const [zonesOpen, setZonesOpen] = useState(false);
  const saveZone = useMutation({
    mutationFn: (z: string) => client!.settingsSaveTimezone(z),
    // the zone label reads ["me"]; patch it on the tap so the list closes
    // onto the chosen zone instead of the old one, and put the old value
    // back if the server refuses the name
    onMutate: async (z) => {
      setZonesOpen(false);
      await qc.cancelQueries({ queryKey: ["me"] });
      const prev = qc.getQueryData<Me>(["me"]);
      patchQuery<Me>(qc, ["me"], z
        ? { timezone: z, timezone_source: "household" }
        : { timezone: prev?.instance_timezone, timezone_source: "instance" });
      return { prev };
    },
    // re-assert the accepted zone on success: with two saves in flight,
    // the first one's rollback must not strand the label on the old zone
    onSuccess: (v, z) => {
      seedSettings(qc, v);
      patchQuery<Me>(qc, ["me"], z
        ? { timezone: z, timezone_source: "household" }
        : { timezone: qc.getQueryData<Me>(["me"])?.instance_timezone,
            timezone_source: "instance" });
    },
    onError: (_e, _z, ctx) => {
      if (ctx?.prev) qc.setQueryData(["me"], ctx.prev);
      // …and if a later save did land, the refetch puts its zone back
      qc.invalidateQueries({ queryKey: ["me"] });
    },
  });
  useEffect(() => {
    if (query.data !== undefined && sched === null) {
      setSched(query.data ?? {});
    }
  }, [query.data, sched]);
  // auto-save like the web: every change posts immediately; an error
  // refetches back to server truth instead of leaving a lying switch
  const save = useMutation({
    mutationFn: (next: EmailSchedule) =>
      client!.settingsSaveSchedule(next),
    onSuccess: (v) => seedSettings(qc, v),
    onError: () => { setSched(null); void query.refetch(); },
  });
  // a state updater must stay pure (React may run it twice); the POST
  // goes out beside the setState, from the same computed value
  const upd = (cad: (typeof CADENCES)[number],
               patch: Record<string, unknown>) => {
    const next: EmailSchedule = { ...sched,
      [cad]: { on: false, hour: 7,
               ...(cad === "weekly" ? { weekday: 0 } : {}),
               ...(sched?.[cad] ?? {}), ...patch } };
    setSched(next);
    save.mutate(next);
  };
  const hourLabel = (h: number) =>
    `${h % 12 === 0 ? 12 : h % 12}:00 ${h < 12 ? "am" : "pm"}`;

  if (viewer)
    return (
      <ScrollView style={s.wrap}>
        <Text style={s.center}>
          The summary schedule is the owner&apos;s. Whether the email
          reaches your inbox is yours — Settings → Daily email.
        </Text>
      </ScrollView>
    );

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 24 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => query.refetch()} />}>
      {query.isPending && <Text style={s.center}>Loading…</Text>}
      {sched && (
        <Text style={[s.center, { fontSize: 11, paddingBottom: 0 }]}>
          times are {effective ? zoneLabel(effective) : "local time"}
          {!household && instanceZone ? " (your instance's zone)" : ""}
        </Text>
      )}
      {/* The zone the hours are kept in — web: Settings → Email & Push "Time
          zone". Blank = follow the instance, right for a box in your own
          closet; a hosted household west of the instance would be mailed
          at 2am unless it can say where it is. */}
      {sched && (
        <Card>
          <H>Time zone</H>
          <Text style={s.lbl}>
            {household ? zoneLabel(household)
              : instanceZone ? `instance default — ${zoneLabel(instanceZone)}`
              : "instance default"}
          </Text>
          <View style={{ flexDirection: "row", gap: 8, flexWrap: "wrap",
                         marginTop: 6 }}>
            {device && device !== effective && (
              <Pressable style={s.btn} disabled={saveZone.isPending}
                         onPress={() => saveZone.mutate(device)}>
                <Text style={s.btnText}>
                  use this phone&apos;s ({zoneLabel(device)})</Text>
              </Pressable>
            )}
            <Pressable style={s.btn} onPress={() => setZonesOpen((o) => !o)}>
              <Text style={s.btnText}>{zonesOpen ? "close" : "change…"}</Text>
            </Pressable>
            {household ? (
              <Pressable style={s.btn} disabled={saveZone.isPending}
                         onPress={() => saveZone.mutate("")}>
                <Text style={s.btnText}>follow the instance</Text>
              </Pressable>
            ) : null}
          </View>
          {zonesOpen && (
            <ScrollView style={{ maxHeight: 260, marginTop: 8 }}
                        nestedScrollEnabled>
              {knownZones([household, instanceZone, device]).map((z) => (
                <Pressable key={z} onPress={() => saveZone.mutate(z)}
                           style={{ paddingVertical: 6 }}>
                  <Text style={{ color: z === effective ? C.text : C.mut,
                                 fontSize: 13,
                                 fontWeight: z === effective ? "700" : "400" }}>
                    {zoneLabel(z)}
                  </Text>
                </Pressable>
              ))}
            </ScrollView>
          )}
        </Card>
      )}
      {sched && CADENCES.map((cad) => {
        const c = (sched[cad] ?? { on: false, hour: 7 }) as
          { on: boolean; hour: number; weekday?: number; sms?: boolean;
            push?: boolean; summary?: boolean };
        // the web's resting-state sentence: when · the channels that
        // actually reach you, or "off"
        const when = cad === "daily"
          ? `every day at ${hourLabel(c.hour ?? 7)}`
          : cad === "weekly"
            ? `every ${WEEKDAYS[c.weekday ?? 0]} at ${
                hourLabel(c.hour ?? 7)}`
          : cad === "monthly"
            ? `on the 1st at ${hourLabel(c.hour ?? 7)}`
            : `on Jan 1 at ${hourLabel(c.hour ?? 7)}`;
        const chans = [
          c.on ? (cad === "daily" && c.summary
            ? "email (summary)" : "email") : null,
          c.sms ? "SMS" : null,
          c.push ? "push" : null,
        ].filter(Boolean).join(", ");
        const open = openCad === cad;
        return (
          <Card key={cad}>
            <View style={s.row}>
              <H>{CADENCE_TITLE[cad]}</H>
              <Text style={{ color: C.accent, fontSize: 13 }}
                    onPress={() => setOpenCad(open ? null : cad)}>
                {open ? "done" : chans ? "change" : "turn on"}
              </Text>
            </View>
            <Text style={{ color: C.mut, fontSize: 12 }}>
              {chans ? `${when} · ${chans}` : "off"}
            </Text>
            {open && (
              <>
                {/* the web's am/pm hour picker, as chips — never a raw
                    0-23 number box */}
                <Text style={[s.lbl, { marginTop: 4 }]}>at</Text>
                <View style={{ flexDirection: "row", flexWrap: "wrap",
                               gap: 6, paddingVertical: 4 }}>
                  {Array.from({ length: 24 }, (_, h) => h).map((h) => (
                    <Pressable key={h}
                      style={[s.day, (c.hour ?? 7) === h && s.dayOn]}
                      onPress={() => upd(cad, { hour: h })}>
                      <Text style={{ color: (c.hour ?? 7) === h
                                       ? C.text : C.mut, fontSize: 11 }}>
                        {hourLabel(h)}
                      </Text>
                    </Pressable>
                  ))}
                </View>
                <View style={s.row}>
                  <Text style={s.lbl}>Email</Text>
                  <Switch value={!!c.on}
                          onValueChange={(on) => upd(cad, { on })}
                          trackColor={{ true: C.accent, false: C.hover }}
                          thumbColor={C.text} />
                </View>
                {cad === "weekly" && (
                  <View style={{ flexDirection: "row", flexWrap: "wrap",
                                 gap: 6, paddingVertical: 4 }}>
                    {WEEKDAYS.map((w, i) => (
                      <Pressable key={w}
                        style={[s.day,
                          ((sched.weekly?.weekday ?? 0) === i) && s.dayOn]}
                        onPress={() => upd("weekly", { weekday: i })}>
                        <Text style={{ color:
                          (sched.weekly?.weekday ?? 0) === i
                            ? C.text : C.mut, fontSize: 12 }}>{w}</Text>
                      </Pressable>
                    ))}
                  </View>
                )}
                <View style={s.row}>
                  <Text style={s.lbl}>Push to phones</Text>
                  <Switch value={!!c.push} disabled={!pushOk}
                          onValueChange={(push) => upd(cad, { push })}
                          trackColor={{ true: C.accent, false: C.hover }}
                          thumbColor={C.text} />
                </View>
                {!pushOk && (
                  <Text style={s.gateHint}>
                    push needs a signed-in phone with notifications
                    allowed
                  </Text>
                )}
                {/* the email's own summary/detail choice — summary = the
                    verdict card alone, detail = the full report.
                    Deliberately NOT linked to the Today page's
                    toggle. */}
                {cad === "daily" && (
                  <>
                    {/* the choice only applies to a sent email — with
                        the email off the control is inert, so it must
                        LOOK inert (the web uses a disabled <select>;
                        here that's the same dim + hint the gated
                        Switches get) */}
                    <View style={[s.row, !c.on && { opacity: 0.5 }]}>
                      <Text style={s.lbl}>Email face</Text>
                      <View style={{ flexDirection: "row",
                                     borderColor: C.border, borderWidth: 1,
                                     borderRadius: 999,
                                     overflow: "hidden" }}>
                        {(["summary", "detail"] as const).map((f) => {
                          const on =
                            (c.summary ? "summary" : "detail") === f;
                          return (
                            <Text key={f}
                                  onPress={c.on ? () =>
                                    upd(cad, { summary: f === "summary" })
                                    : undefined}
                                  style={{ paddingHorizontal: 12,
                                           paddingVertical: 4,
                                           fontSize: 12,
                                           backgroundColor: on ? C.hover
                                                               : undefined,
                                           color: on ? C.text : C.mut,
                                           fontWeight: on ? "600"
                                                          : "400" }}>
                              {f}
                            </Text>
                          );
                        })}
                      </View>
                    </View>
                    {!c.on && (
                      <Text style={s.gateHint}>
                        turn the email on to choose its face
                      </Text>
                    )}
                  </>
                )}
                <View style={s.row}>
                  <Text style={s.lbl}>SMS</Text>
                  <Switch value={!!c.sms} disabled={!smsOk}
                          onValueChange={(sms) => upd(cad, { sms })}
                          trackColor={{ true: C.accent, false: C.hover }}
                          thumbColor={C.text} />
                </View>
                {!smsOk && (
                  <Text style={s.gateHint}>
                    {notify.data?.sms_available
                      ? "verify a phone number below first"
                      : "SMS isn't configured on this server"}
                  </Text>
                )}
              </>
            )}
          </Card>
        );
      })}
      {/* changes save themselves (the web idiom) — the only thing left
          to say is when one didn't */}
      {save.isError ? (
        <Text style={[s.center, { color: C.bad }]}>
          Couldn&apos;t save — reloaded the server&apos;s settings.
        </Text>
      ) : null}
      {(notify.data?.native_push_devices ?? 0) > 0 && (
        <Text style={[s.center, { fontSize: 11 }]}>
          {notify.data!.native_push_devices} phone
          {notify.data!.native_push_devices === 1 ? "" : "s"} registered
          via the mobile app
        </Text>
      )}
      <RecipientsCard schedule={sched} />
      <PhoneCard />
      <SendNowCard />
    </ScrollView>
  );
}

// ---- who receives the email — every add/remove/mute IS the save
// (the web's auto-save idiom); the complete trio posts each time so
// rapid edits are last-write-wins under the server's settings lock ----
type RecipStatus = {
  email: string; owner: boolean; muted?: boolean;
  delivery: "bouncing" | "complained" | null;
  delivery_reason: string | null;
  invite?: "invited" | "accepted" | "declined" | "expired" | null;
  via?: string;
};
function RecipientsCard({ schedule }: {
  schedule: EmailSchedule | null;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const d = settings.data as (Record<string, unknown> | undefined);
  const recipients = (d?.email_recipients as string[] | null) ?? [];
  const muted = ((d?.email_muted as string[] | null) ?? [])
    .map((x) => x.toLowerCase());
  const status = (d?.email_recipient_status as RecipStatus[] | null)
    ?? [];
  const [draft, setDraft] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [resending, setResending] = useState<string | null>(null);
  const save = useMutation({
    // never post an EMPTY schedule — before the schedule query resolves
    // that would wipe every cadence; the card waits instead
    mutationFn: (body: { email_recipients: string[];
                         email_muted: string[] }) => {
      if (schedule === null)
        throw new Error("still loading — try again in a second");
      return client!.settingsSave({ ...body,
        email_schedule: schedule });
    },
    // the list, the mute marks and the new row all read ["settings"]:
    // patch it on the tap so they move at once, and let the reply (the
    // full view, with the server's recipient status) settle it
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: ["settings"] });
      const prev = qc.getQueryData<SettingsView>(["settings"]);
      patchQuery<SettingsView>(qc, ["settings"], body);
      return { prev };
    },
    onSuccess: (v) => seedSettings(qc, v),
    onError: (e, _b, ctx) => {
      setErr(errText(e));
      // a rejected save must snap back to server truth
      if (ctx?.prev) qc.setQueryData(["settings"], ctx.prev);
      else qc.invalidateQueries({ queryKey: ["settings"] });
    },
  });
  const setMuted = (email: string, on: boolean) => {
    const low = email.toLowerCase();
    save.mutate({ email_recipients: recipients,
      email_muted: on ? [...new Set([...muted, low])]
                      : muted.filter((x) => x !== low) });
  };
  const remove = (email: string) =>
    save.mutate({ email_recipients:
        recipients.filter((x) => x !== email),
      email_muted: muted });
  const add = () => {
    const v = draft.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v)) {
      setErr("That doesn't look like an email address."); return;
    }
    if (recipients.some((x) => x.toLowerCase() === v.toLowerCase())) {
      setErr("Already on the list."); setDraft(""); return;
    }
    setErr(null); setDraft("");
    save.mutate({ email_recipients: [...recipients, v],
                  email_muted: muted });
  };
  const badge = (st: RecipStatus | undefined,
                 isOwner: boolean): [string, string] => {
    if (!st) return ["not saved yet", C.mut];
    if (st.delivery)
      return [st.delivery === "complained"
        ? "marked as spam" : "not arriving", C.bad];
    if (isOwner) return ["you", C.mut];
    if (st.via === "member") return ["household member", C.mut];
    if (st.invite === "accepted") return ["invite accepted", C.good];
    if (st.invite === "invited") return ["invite sent", C.warn];
    if (st.invite === "expired") return ["invite expired", C.warn];
    if (st.invite === "declined") return ["declined", C.mut];
    return ["not invited", C.warn];
  };
  const ownerRow = status.find((x) => x.owner);
  const rows: { email: string; removable: boolean }[] = [
    ...(ownerRow ? [{ email: ownerRow.email, removable: false }] : []),
    ...recipients.map((r) => ({ email: r, removable: true })),
  ];
  return (
    <Card>
      <H>Who receives it</H>
      {rows.map(({ email, removable }) => {
        const st = status.find(
          (x) => x.email.toLowerCase() === email.toLowerCase());
        const isMuted = muted.includes(email.toLowerCase());
        const [label, color] = badge(st, !removable);
        // a bouncing-but-accepted recipient keeps Resend — accepting
        // clears the bounce, so re-inviting is the recovery path
        const canResend = removable && st
          && (st.delivery
              || (st.invite !== "accepted" && st.invite !== "declined"));
        return (
          <View key={email}
                style={[s.row, { paddingVertical: 6, gap: 8,
                                 justifyContent: "flex-start",
                                 opacity: isMuted ? 0.6 : 1 }]}>
            <Text style={{ fontSize: 16,
                           color: isMuted ? C.mut : C.accent }}
                  onPress={() => setMuted(email, !isMuted)}>
              {isMuted ? "☐" : "☑"}
            </Text>
            <View style={{ flex: 1 }}>
              <Text style={{ color: C.text, fontSize: 13 }}
                    numberOfLines={1}>{email}</Text>
              <Text style={{ color, fontSize: 11 }}>
                {isMuted ? "not receiving" : label}
              </Text>
            </View>
            {canResend ? (
              <Text style={{ color: C.accent, fontSize: 12, padding: 4 }}
                    onPress={() => {
                      setResending(email);
                      client!.recipientInviteResend(email)
                        .then(() => {
                          // the badge flips to "invite sent" now; the
                          // refetch brings the server's full status
                          patchList<SettingsView, RecipStatus>(qc,
                            ["settings"], "email_recipient_status",
                            (x) => x.email.toLowerCase()
                              === email.toLowerCase(),
                            { invite: "invited" });
                          qc.invalidateQueries({ queryKey: ["settings"] });
                        })
                        .catch((e) =>
                          setErr(errText(e)))
                        .finally(() => setResending(null));
                    }}>
                {resending === email ? "sending…"
                  : st?.invite ? "Resend" : "Invite"}
              </Text>
            ) : null}
            {removable && (
              <Text style={{ color: C.mut, fontSize: 12, padding: 4 }}
                    onPress={() => remove(email)}>
                remove
              </Text>
            )}
          </View>
        );
      })}
      <View style={{ flexDirection: "row", gap: 8, marginTop: 6 }}>
        <TextInput style={[s.hour, { flex: 1, textAlign: "left" }]}
                   placeholder="partner@example.com"
                   placeholderTextColor={C.mut} autoCapitalize="none"
                   keyboardType="email-address"
                   value={draft} onChangeText={setDraft}
                   onSubmitEditing={add} />
        <Pressable style={[s.btn, !draft.trim() && { opacity: 0.5 }]}
                   disabled={!draft.trim()} onPress={add}>
          <Text style={s.btnText}>Add</Text>
        </Pressable>
      </View>
      {draft.trim() && !err ? (
        <Text style={s.gateHint}>
          Not added yet — press Add to save and invite.
        </Text>
      ) : null}
      {err ? (
        <Text style={{ color: C.bad, fontSize: 12 }}
              onPress={() => setErr(null)}>{err}</Text>
      ) : null}
      <Text style={[s.gateHint, { textAlign: "left" }]}>
        Adding someone emails them an invitation right away — they
        start receiving the summary once they accept.
      </Text>
    </Card>
  );
}

// SMS number: verify + test + remove. The consent block is compliance
// surface — one checkbox per message program, gating the send-code
// button; deliberately not persisted, ticked in the same interaction
// that submits the number.
function PhoneCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const notify = useQuery({
    queryKey: ["notify-status"],
    queryFn: () => client!.notifyStatus(),
    enabled: !!client,
  });
  const [phone, setPhone] = useState("");
  const [code, setCode] = useState("");
  const [consent, setConsent] = useState(false);
  // a test SMS is a real (paid) text — one tap, one message; the web
  // button disables the same way while its request is in flight
  const [testing, setTesting] = useState(false);
  // send-code and verify are paid/real actions too — one tap, one request
  const [busy, setBusy] = useState<"send" | "verify" | "remove" | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const n = notify.data;
  type NotifyStatus = Awaited<ReturnType<Client["notifyStatus"]>>;
  const done = (patch: Partial<NotifyStatus>) => {
    patchQuery<NotifyStatus>(qc, ["notify-status"], patch);
    qc.invalidateQueries({ queryKey: ["notify-status"] });
  };
  if (!n || !n.sms_available) {
    if (n && n.sms_entitled === false)
      return (
        <Text style={[s.center, { fontSize: 12 }]}>
          Text alerts aren&apos;t available on this account — your daily
          verdict and alerts arrive by email and push.
        </Text>
      );
    if (n)
      // say why the section is empty, like the web — a silently missing
      // channel reads as a broken app
      return (
        <Text style={[s.center, { fontSize: 12 }]}>
          SMS: not configured on this instance (needs Twilio credentials
          in the environment).
        </Text>
      );
    return null;
  }
  return (
    <Card>
      <H>SMS number</H>
      {n.phone_verified ? (
        <>
          <Text style={{ color: C.text, fontSize: 14 }}>
            {n.phone}{" "}
            <Text style={{ color: C.good, fontSize: 12 }}>verified</Text>
          </Text>
          <View style={{ flexDirection: "row", gap: 8, marginTop: 6 }}>
            <Pressable style={[s.btn, testing && { opacity: 0.5 }]}
                       disabled={testing}
                       onPress={() => { setTesting(true);
                         client!.notifyTest("sms")
                           .then(() => setMsg("Test sent."))
                           .catch((e) => setMsg(errText(e)))
                           .finally(() => setTesting(false)); }}>
              <Text style={s.btnText}>
                {testing ? "sending…" : "send test SMS"}
              </Text>
            </Pressable>
            <Pressable style={[s.btn, { backgroundColor: C.hover },
                               busy === "remove" && { opacity: 0.5 }]}
                       disabled={busy !== null}
                       onPress={() => { setBusy("remove");
                         client!.notifyPhone("")
                           .then(() => done({ phone: null,
                                              phone_verified: false }))
                           .catch((e) => setMsg(errText(e)))
                           .finally(() => setBusy(null)); }}>
              <Text style={[s.btnText, { color: C.mut }]}>
                {busy === "remove" ? "removing…" : "remove"}
              </Text>
            </Pressable>
          </View>
        </>
      ) : (
        <>
          <Pressable style={{ flexDirection: "row", gap: 8,
                              alignItems: "flex-start" }}
                     onPress={() => setConsent((x) => !x)}>
            <Text style={{ color: consent ? C.accent : C.mut,
                           fontSize: 16 }}>
              {consent ? "☑" : "☐"}
            </Text>
            <Text style={{ color: C.text, fontSize: 12, flex: 1,
                           lineHeight: 17 }}>
              <Text style={{ fontWeight: "700" }}>
                Yes, text me my budget verdict.{" "}
              </Text>
              I agree to receive recurring automated texts from Oikonome
              telling me whether I am on budget, on the schedule I set
              above — about one message a day, up to ~30/month. Msg &amp;
              data rates may apply. Reply STOP to cancel or HELP for
              help.
            </Text>
          </Pressable>
          <Text style={[s.gateHint, { textAlign: "left" }]}>
            Consent is not a condition of purchase and is not required
            to use Oikonome.
          </Text>
          <Text style={[s.gateHint, { textAlign: "left" }]}>
            {ext.legal.terms && ext.legal.privacy ? (
              <>
                Separately, our{" "}
                <Text style={{ color: C.accent }}
                      onPress={() => Linking.openURL(ext.legal.terms!)}>
                  Terms
                </Text>
                {" and "}
                <Text style={{ color: C.accent }}
                      onPress={() => Linking.openURL(ext.legal.privacy!)}>
                  Privacy Policy
                </Text>
                {" apply to your use of Oikonome. "}
              </>
            ) : null}
            We never sell or share your phone number.
          </Text>
          <View style={{ flexDirection: "row", gap: 8, marginTop: 6 }}>
            <TextInput style={[s.hour, { flex: 1, textAlign: "left" }]}
                       placeholder="+12175551234"
                       placeholderTextColor={C.mut}
                       keyboardType="phone-pad"
                       value={phone} onChangeText={setPhone} />
            <Pressable style={[s.btn,
                         (!phone.trim() || !consent || busy !== null)
                           && { opacity: 0.5 }]}
                       disabled={!phone.trim() || !consent || busy !== null}
                       onPress={() => { setBusy("send");
                         client!.notifyPhone(phone.trim(),
                                             ["summary"])
                           .then(() => { setMsg("Code sent — enter it "
                                                + "below.");
                                         done({ phone_pending:
                                                  phone.trim() }); })
                           .catch((e) => setMsg(errText(e)))
                           .finally(() => setBusy(null)); }}>
              <Text style={s.btnText}>
                {busy === "send" ? "sending…"
                  : n.phone_pending ? "re-send code" : "send code"}
              </Text>
            </Pressable>
          </View>
          {n.phone_pending && (
            <View style={{ flexDirection: "row", gap: 8, marginTop: 6 }}>
              <TextInput style={[s.hour, { flex: 1, textAlign: "left" }]}
                         placeholder="6-digit code"
                         placeholderTextColor={C.mut}
                         keyboardType="number-pad" maxLength={6}
                         value={code} onChangeText={setCode} />
              <Pressable style={[s.btn,
                           (!code || busy !== null) && { opacity: 0.5 }]}
                         disabled={!code || busy !== null}
                         onPress={() => { setBusy("verify");
                           client!.notifyPhoneVerify(code)
                             .then(() => { setMsg("Phone verified.");
                                           setCode("");
                                           done({ phone: n.phone_pending,
                                                  phone_verified: true,
                                                  phone_pending: null }); })
                             .catch((e) => setMsg(errText(e)))
                             .finally(() => setBusy(null)); }}>
                <Text style={s.btnText}>
                  {busy === "verify" ? "verifying…" : "verify"}
                </Text>
              </Pressable>
            </View>
          )}
        </>
      )}
      {msg ? (
        <Text style={{ color: msg.endsWith(".") && !msg.includes(":")
                         ? C.good : C.bad, fontSize: 12, marginTop: 6 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      ) : null}
    </Card>
  );
}

function SendNowCard() {
  const { client } = useSession();
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return (
    <Card>
      <H>Send today&apos;s email now</H>
      <Text style={s.gateHint}>
        sends to everyone on the recipient list, even if the daily
        toggle is off; SMS/push don&apos;t fire and the scheduled send
        still happens
      </Text>
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 6 },
                   busy && { opacity: 0.5 }]}
                 disabled={busy}
                 onPress={() => { setBusy(true);
                   client!.jobsEmail()
                     .then((r) =>
                       setMsg(`Sent to your recipients: ${r.subject}`))
                     .catch((e) => setMsg(errText(e)))
                     .finally(() => setBusy(false)); }}>
        <Text style={s.btnText}>
          {busy ? "sending…" : "Send today's email now"}
        </Text>
      </Pressable>
      {msg ? (
        <Text style={{ color: msg.startsWith("Sent") ? C.good : C.bad,
                       fontSize: 12, marginTop: 6 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      ) : null}
    </Card>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  center: { color: C.mut, textAlign: "center", padding: 20 },
  row: { flexDirection: "row", alignItems: "center",
         justifyContent: "space-between", paddingVertical: 4 },
  lbl: { color: C.mut, fontSize: 14 },
  hour: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 8, color: C.text, fontSize: 14, minWidth: 56,
          paddingHorizontal: 10, paddingVertical: 6, textAlign: "center" },
  save: { backgroundColor: C.accent, borderRadius: 10, marginHorizontal: 12,
          marginTop: 8, paddingVertical: 12, alignItems: "center" },
  day: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
         borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  dayOn: { borderColor: C.accent, backgroundColor: C.hover },
  gateHint: { color: C.mut, fontSize: 11, textAlign: "right",
              marginTop: -2 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8 },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
});
