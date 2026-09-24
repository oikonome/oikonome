// Settings — who you are, this device, the add-on's account section,
// support access, script tokens (self-host), sign out.
// Deeper posture changes live on the Security & household screen.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import * as Clipboard from "expo-clipboard";
import Constants from "expo-constants";
import { useRef, useState } from "react";
import { Alert, Pressable, ScrollView, StyleSheet, Switch, Text, TextInput,
         View } from "react-native";
import PullRefresh from "../components/pull-refresh";

import { Card, H, HelpLink, KV } from "../components/ui";
import { ext } from "../ext";

// the add-on's account card, fixed for the life of the bundle
const AccountCard = ext.account?.Card;
import * as DocumentPicker from "expo-document-picker";
import { router, useRouter } from "expo-router";

import { Continuity, errText, Me, PickedFile, SettingsView, TokenScope, Webhook,
         WebhookEvent } from "../lib/api";
import { patchQuery, removeFromList, saveSettings } from "../lib/cache";
import { signOutAction } from "../lib/pure";
import { useSession } from "../lib/session";
import { useOwner, useViewer } from "../lib/viewer";
import { C, ThemeMode, getThemeMode, mmddyy, setThemeMode,
         themeRestartPending } from "../lib/theme";

// the web Settings' section headers — same names, same order
function SectionHead({ children }: { children: React.ReactNode }) {
  return (
    <Text style={{ color: C.mut, fontSize: 11,
                   textTransform: "uppercase", letterSpacing: 1,
                   marginHorizontal: 12, marginTop: 16 }}>
      {children}
    </Text>
  );
}
function NavRow({ label, sub, href, warn }: {
  label: string; sub: string; href: string; warn?: boolean;
}) {
  const router = useRouter();
  return (
    <Pressable onPress={() => router.push(href as never)}
               style={({ pressed }) => [{
                 backgroundColor: pressed ? C.hover : C.card,
                 borderColor: C.border, borderWidth: 1,
                 borderRadius: C.r, marginHorizontal: 12, marginTop: 8,
                 paddingHorizontal: 14, paddingVertical: 11,
                 flexDirection: "row", alignItems: "center", gap: 8 }]}>
      <View style={{ flex: 1 }}>
        <Text style={{ color: C.text, fontSize: 14,
                       fontWeight: "600" }}>{label}</Text>
        <Text style={{ color: warn ? C.warn : C.mut, fontSize: 12 }}>
          {sub}
        </Text>
      </View>
      <Text style={{ color: C.mut, fontSize: 18 }}>›</Text>
    </Pressable>
  );
}

const cents = (c?: number) =>
  c == null ? "—" : `$${(c / 100).toFixed(c % 100 === 0 ? 0 : 2)}`;

// the guarded cards share one error shape: translate the raw error into
// the card's own message (first matching phrase wins). Identity itself is
// no longer this screen's job — a refused call raises the app-wide
// "confirm it's you" sheet and retries (lib/api.ts, session elevation).
function stepUpMsg(e: unknown, phrases: [string, string][] = []): string {
  // errText strips the "Error: NNN:" wrapper, so the phrase match and the
  // fallback message both read the server's own words
  const t = errText(e);
  for (const [needle, friendly] of phrases)
    if (t.includes(needle)) return friendly;
  return t;
}

// The export is the household's whole ledger, so the ticket door wants
// FRESH elevation (the sheet shows even inside a live window) behind a
// native confirm; a session alone can never fetch it.
function ExportDownloads({ dump }: { dump: boolean }) {
  const { client } = useSession();
  const [busy, setBusy] = useState<"zip" | "dump" | null>(null);
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(
    null);
  const go = async (kind: "zip" | "dump") => {
    setBusy(kind); setMsg(null);
    try {
      const t = await client!.exportTicket(kind);
      await client!.download(t.url, kind === "zip"
        ? "oikonome-export.zip" : "oikonome-dump.sql.gz");
      setMsg({ text: "Downloaded." });
    } catch (e) {
      setMsg({ text: stepUpMsg(e), bad: true });
    }
    setBusy(null);
  };
  const ask = (kind: "zip" | "dump") => Alert.alert(
    kind === "zip" ? "Download the full export?"
                   : "Download the database dump?",
    "This is everything in the household, in one file. You'll confirm "
    + "it's you first, and the file goes to the share sheet.",
    [{ text: "Download", onPress: () => go(kind) },
     { text: "Cancel", style: "cancel" }]);
  return (
    <View>
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 6 },
                         !!busy && { opacity: 0.5 }]}
                 disabled={!!busy} onPress={() => ask("zip")}>
        <Text style={s.btnText}>
          {busy === "zip" ? "downloading…" : "Download export ZIP"}</Text>
      </Pressable>
      {dump && (
        <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 6 },
                           !!busy && { opacity: 0.5 }]}
                   disabled={!!busy} onPress={() => ask("dump")}>
          <Text style={s.btnText}>
            {busy === "dump" ? "downloading…"
              : "Download database dump (.sql.gz)"}</Text>
        </Pressable>
      )}
      {msg && <Text style={[s.hint, msg.bad && { color: C.warn }]}>
        {msg.text}</Text>}
    </View>
  );
}

// The continuity packet (web: Settings.tsx ContinuityCard): the
// household's money on paper for whoever is left to run it. Two fields
// the owner writes, the PDF through the export ticket + share sheet, and
// an emailed copy that steps up like the download.
function ContinuityCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["continuity"],
                       queryFn: () => client!.continuity(), enabled: !!client });
  const [note, setNote] = useState<string | null>(null);
  const [who, setWho] = useState<string | null>(null);
  const [to, setTo] = useState("");
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const d = q.data;
  const noteNow = note ?? d?.note ?? "";
  const whoNow = who ?? d?.prepared_for ?? "";
  const dirty = !!d && (noteNow !== d.note || whoNow !== d.prepared_for);
  const save = useMutation({
    mutationFn: () => client!.continuitySave({ note: noteNow,
                                               prepared_for: whoNow }),
    onSuccess: (r) => {
      qc.setQueryData<Continuity>(["continuity"], (o) => o && { ...o, ...r });
      setNote(null); setWho(null); setMsg({ text: "Saved." });
    },
    onError: (e) => setMsg({ text: stepUpMsg(e), bad: true }),
  });
  const get = useMutation({
    mutationFn: async () => {
      const t = await client!.exportTicket("continuity");
      await client!.download(t.url, "oikonome-continuity.pdf");
    },
    onSuccess: () => setMsg({ text: "Downloaded." }),
    onError: (e) => setMsg({ text: stepUpMsg(e), bad: true }),
  });
  const mail = useMutation({
    mutationFn: () => client!.continuityEmail(to.trim()),
    onSuccess: (r) => setMsg({ text: `Sent to ${r.to}.` }),
    onError: (e) => setMsg({ text: stepUpMsg(e), bad: true }),
  });
  const busy = save.isPending || get.isPending || mail.isPending;
  const askMail = () => Alert.alert(
    "Email the continuity packet?",
    `It goes to ${to.trim()} — the whole shape of the household's money. `
    + "You'll confirm it's you first.",
    [{ text: "Send", onPress: () => mail.mutate() },
     { text: "Cancel", style: "cancel" }]);
  return (
    <Card>
      <H>Continuity packet</H>
      <Text style={s.hintL}>
        If something happens to you, the person left running the household
        needs a map: every account and where it is held, the balances that
        day, the bills and what pays them, the income, the businesses — and,
        in your words, where the rest is (the password manager, the will,
        the lawyer, who to call). One PDF from what Oikonome already knows.
        It holds no passwords.
      </Text>
      <Text style={[s.hintL2, { marginTop: 10 }]}>Prepared for (optional)</Text>
      <TextInput style={s.input} placeholder="a name, e.g. Sam"
                 placeholderTextColor={C.mut} maxLength={120}
                 value={whoNow} onChangeText={setWho} />
      <Text style={[s.hintL2, { marginTop: 10 }]}>
        Where the rest is — printed as the first section</Text>
      <TextInput style={[s.input, { minHeight: 110, textAlignVertical: "top" }]}
                 multiline maxLength={d?.note_max ?? 4000}
                 placeholder={"Passwords: in the family password manager; "
                   + "the recovery kit is in the safe.\nWill and life "
                   + "insurance: top drawer of the desk; the lawyer is…\n"
                   + "Call…"}
                 placeholderTextColor={C.mut}
                 value={noteNow} onChangeText={setNote} />
      <View style={{ flexDirection: "row", gap: 8, marginTop: 8,
                     flexWrap: "wrap" }}>
        <Pressable style={[s.btn, s.btnQuiet, (!dirty || busy) && { opacity: 0.5 }]}
                   disabled={!dirty || busy} onPress={() => save.mutate()}>
          <Text style={s.btnText}>{save.isPending ? "saving…" : "Save"}</Text>
        </Pressable>
        <Pressable style={[s.btn, (busy || !d) && { opacity: 0.5 }]}
                   disabled={busy || !d} onPress={() => get.mutate()}>
          <Text style={s.btnText}>
            {get.isPending ? "preparing…" : "Download PDF"}</Text>
        </Pressable>
      </View>
      {d && (
        <View style={{ marginTop: 12 }}>
          <Text style={s.hintL2}>
            Email a copy{d.members_only ? " to someone in the household" : ""}
          </Text>
          {d.members_only ? (
            <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6,
                           marginTop: 6 }}>
              {d.members.map((m) => (
                <Pressable key={m} style={[s.chip, to === m && s.chipOn]}
                           onPress={() => setTo(m)}>
                  <Text style={{ color: C.text, fontSize: 12 }}>{m}</Text>
                </Pressable>
              ))}
            </View>
          ) : (
            <TextInput style={s.input} placeholder="their email address"
                       placeholderTextColor={C.mut} autoCapitalize="none"
                       keyboardType="email-address" value={to}
                       onChangeText={setTo} />
          )}
          <Pressable style={[s.btn, s.btnQuiet, { alignSelf: "flex-start",
                              marginTop: 8 },
                             (busy || !to.trim() || !d.mail_configured)
                               && { opacity: 0.5 }]}
                     disabled={busy || !to.trim() || !d.mail_configured}
                     onPress={askMail}>
            <Text style={s.btnText}>
              {mail.isPending ? "sending…" : "Email the packet"}</Text>
          </Pressable>
          {!d.mail_configured && (
            <Text style={[s.hintL2, { marginTop: 6 }]}>
              Email isn't configured on this instance — download the PDF and
              hand it over instead.</Text>
          )}
          {d.members_only && (
            <Text style={[s.hintL2, { marginTop: 6 }]}>
              Hosted mails the household's finances only to an address with
              an account here. Invite them under Users first, or download
              the PDF and hand it over.</Text>
          )}
        </View>
      )}
      {msg && <Text style={[s.hintL, { marginTop: 8,
                                       color: msg.bad ? C.warn : C.good }]}>
        {msg.text}</Text>}
    </Card>
  );
}

export default function Settings() {
  const { client, serverUrl, signOut } = useSession();
  const [testMsg, setTestMsg] = useState<string | null>(null);
  const testPush = useMutation({
    mutationFn: () => client!.notifyTest("push"),
    onSuccess: (d) => setTestMsg(
      d.delivered ? `Sent — delivered to ${d.delivered} target${
        d.delivered === 1 ? "" : "s"}.` : "Sent."),
    onError: (e) => setTestMsg(errText(e)),
  });
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => client!.me(),
    enabled: !!client,
  });
  const devices = useQuery({
    queryKey: ["devices"],
    queryFn: () => client!.devices(),
    enabled: !!client,
  });
  // the sign-out dialog promised a revoked token; when that can't be
  // delivered, the broken promise is said out loud instead of papered over
  const offerLocalSignOut = () => {
    Alert.alert("Couldn't revoke this device's token",
      "The server couldn't confirm the revocation. Signing out locally "
      + "leaves this device's token active until it expires or is "
      + "revoked from another device (Security & household → Sessions).",
      [{ text: "Cancel", style: "cancel" },
       { text: "Sign out locally anyway", style: "destructive",
         onPress: () => signOut() }]);
  };
  const revoke = useMutation({
    mutationFn: (id: string) => client!.revokeSelf(id),
    onSuccess: (_d, id) => {
      const current = devices.data?.devices.find((d) => d.id === id)?.current;
      if (current) signOut();
      else removeFromList(qc, ["devices"], "devices",
                          (d: { id: string }) => d.id === id);
    },
    // a failed revoke must not strand the user with no way out — but
    // signing out locally over it has to be a stated choice, because
    // the token it leaves behind stays valid
    onError: offerLocalSignOut,
  });

  // the web's settings search + status subtitles — computed from
  // queries the app already keeps warm, never extra fetches per row
  const [filter, setFilter] = useState("");
  // reads are demo-safe — the sections render (untouchable) on demo too
  const owner = me.data?.role === "owner";
  const connsQ = useQuery({ queryKey: ["connections"],
    queryFn: () => client!.connections(), enabled: !!client && owner });
  const schedQ = useQuery({ queryKey: ["schedule"],
    queryFn: () => client!.settingsSchedule(),
    enabled: !!client && owner });
  const q2 = filter.trim().toLowerCase();
  const hit = (words: string) =>
    q2 === "" || words.toLowerCase().includes(q2);
  const hits = {
    conn: hit("connections providers plaid mx simplefin bank keys "
      + "scripts manage accounts rename hide remove add link reconnect"),
    tokens: hit("script tokens api bearer collectors community push import"),
    webhooks: hit("webhooks webhook events post url signature home assistant "
      + "automation matrix integrations outbound"),
    readtokens: hit("integration tokens read token api bearer prometheus "
      + "metrics grafana home assistant sensor mcp assistant scrape "
      + "endpoints summary"),
    move: hit("move fresh environment bundle passphrase connections "
      + "backup import"),
    continuity: hit("continuity packet if i die spouse partner survivor "
      + "estate emergency pdf where things are will print"),
    smtp: hit("email delivery smtp host port password sender test"),
    daily: hit("daily email verdict recipients schedule cadence weekly "
      + "monthly report send email now sms push"),
    family: hit("users family household members invite viewer role"),
    billing: !!ext.account && hit(ext.account.keywords),
    support: hit("support access grant operator help data consent"),
    feedback: hit("feedback bug report reports"),
    system: hit("system health doctor checks diagnostics collectors "
      + "scripts bundle"),
    rules: hit("rules categorization learn model seed correct disable rename "
      + "custom category"),
    merchants: hit("merchant merchants payee rename merge logos icons "
      + "minimal location map where details"),
    llm: hit("ai assistant smart categorization llm ollama openai model "
      + "endpoint api key extra body vision receipts"),
    setup: hit("setup wizard walkthrough guided onboarding welcome tour "
      + "re-run rerun restart redo first run retirement retire business "
      + "birthdate"),
    // the two on-device switches (biometric gate, capture block) live on
    // the Security & household screen with everything else security, so
    // their words point there too
    security: hit("security password two-factor totp 2fa passkeys "
      + "sessions devices signed in sign out delete account erase "
      + "biometric face id fingerprint lock screenshot screen recording "
      + "capture this device"),
    prefs: hit("preferences theme dark light appearance home page default "
      + "landing screen opens"),
  };
  // the web's status board, on the rows the app can answer cheaply
  const nConns = (connsQ.data?.connections ?? []).length;
  const nBad = (connsQ.data?.connections ?? [])
    .filter((c) => c.status && c.status !== "ok").length;
  const connSub = connsQ.data
    ? `${nConns} linked${nBad ? ` · ${nBad} need${
        nBad === 1 ? "s" : ""} attention` : ""}`
    : null;
  const connWarn = nBad > 0;
  const schedSub = (() => {
    const d0 = schedQ.data?.daily as
      { on?: boolean; hour?: number } | undefined;
    if (!d0?.on) return null;
    const h = d0.hour ?? 7;
    return `daily ${h % 12 === 0 ? 12 : h % 12}${h < 12 ? "am" : "pm"}`;
  })();

  // The dialog promises "this device's token is revoked", so the
  // decision can't ride whatever the devices query happens to hold at
  // tap time — a fast tap or a failed fetch would silently take the
  // local-only path and leave the 90-day bearer valid server-side.
  // signOutAction (pure, tested) names the three honest outcomes; an
  // unloaded list gets one refetch before anything is decided.
  const doSignOut = async () => {
    let plan = signOutAction(devices.isSuccess, devices.data?.devices);
    if (plan.kind === "ask") {
      const r = await devices.refetch();
      plan = signOutAction(!!r.data, r.data?.devices);
    }
    if (plan.kind === "revoke") revoke.mutate(plan.id);
    else if (plan.kind === "local") signOut();
    else offerLocalSignOut();
  };
  const confirmSignOut = () => {
    Alert.alert("Sign out", "This device's token is revoked and the app "
      + "returns to the login screen.", [
      { text: "Cancel", style: "cancel" },
      { text: "Sign out", style: "destructive",
        onPress: () => { void doSignOut(); } },
    ]);
  };

  return (
    <ScrollView style={s.wrap} contentContainerStyle={{ paddingBottom: 32 }}
      refreshControl={<PullRefresh tintColor={C.accent} colors={[C.accent]}
                                      progressBackgroundColor={C.card}
                                      onRefresh={() => Promise.all([
                                        me.refetch(), devices.refetch()])} />}>
      <Card>
        <H>Account</H>
        <KV k="Signed in as" v={me.data?.email ?? "…"} />
        <KV k="Role" v={me.data?.role ?? "…"} tone="mut" />
        <KV k="Server" v={serverUrl ?? "…"} tone="mut" />
        <KV k="Server version" v={me.data?.version ?? "…"} tone="mut" />
        {/* read from app.json at build time — the fast way to check
            what's actually installed, and it can't drift from the
            version the build was cut with */}
        <KV k="App build" v={Constants.expoConfig?.version ?? "…"}
            tone="mut" />
      </Card>
      <Card>
        <H>This household's devices</H>
        {(devices.data?.devices ?? []).map((d) => (
          <KV key={d.id}
              k={`${d.device_name}${d.current ? " (this device)" : ""}`}
              v={d.last_seen ? `seen ${mmddyy(d.last_seen)}`
                             : `added ${mmddyy(d.created_at)}`}
              tone={d.current ? undefined : "mut"} />
        ))}
        <Text style={s.hint}>
          Revoke another device under Security &amp; household →
          Sessions; this phone&apos;s biometric lock and screenshot block
          are there too.
        </Text>
      </Card>
      {/* a member's own daily-email switch — the web's MyEmailCard: the
          household schedule is the owner's, whether it reaches THIS inbox
          is the member's */}
      {me.data?.role !== "owner" && <MyEmailCard />}
      {/* owner chrome, like the web — a viewer reads the household but
          does not walk off with its full export */}
      {me.data?.role === "owner" && (
        <Card>
          <H>Your data</H>
          <Text style={s.hintL}>
            Everything is exportable, always: the full export ZIP — every
            table as CSV, with a README that maps what each file is.
            {me.data && !me.data.hosted
              ? " The database dump restores with ./oikonome.sh "
                + "restore <file>."
              : ""}
          </Text>
          <ExportDownloads dump={!me.data.hosted && me.data.role === "owner"} />
        </Card>
      )}
      {me.data?.role === "owner" && !me.data.demo && hits.continuity && (
        <ContinuityCard />
      )}
      <Card>
        <H>Notifications</H>
        <Text style={s.hint}>
          Cadences and channels live under More → Email &amp; Push;
          this phone receives push once signed in.
        </Text>
        <Pressable style={[s.testBtn, testPush.isPending && { opacity: 0.5 }]}
                   disabled={testPush.isPending}
                   onPress={() => { setTestMsg(null); testPush.mutate(); }}>
          <Text style={{ color: C.text, fontWeight: "600" }}>
            Send test push to my devices
          </Text>
        </Pressable>
        {testMsg && (
          <Text style={[s.hint, { color: C.good }]}
                onPress={() => setTestMsg(null)}>{testMsg}</Text>
        )}
      </Card>
      {/* owner-only, never on demo — the web shows viewers only
          Users + Security, and demo is read-only at the server. The
          sections mirror the web Settings page; a card lives inline,
          a full screen links out — same /api/settings keys, so a
          change here IS a change there. */}
      {/* demo shows the whole page, untouchable, under the banner —
          the web renders the same cards inside a disabled fieldset */}
      {me.data?.demo && me.data.role === "owner" && (
        <Text style={[s.hint, { color: C.warn }]}>
          Demo instance — settings are read-only. Everything here is
          shown exactly as the real app renders it, but nothing can be
          changed: the login is shared, so a change would follow the
          next visitor. Explore freely; the data is synthetic.
        </Text>
      )}
      {/* A member gets the settings that are the household's MONEY —
          rules, merchants, the roster, their own security. The rows left
          out are the ACCOUNT: connections and their keys, the mail
          plumbing, exports and bundles, the AI endpoint, billing, the
          instance's health, the setup wizard. Each of those 403s on
          save, and a control that quietly does nothing is worse than one
          that is not there. */}
      {!!me.data && me.data.role !== "viewer" && (
      <View pointerEvents={me.data.demo ? "none" : "auto"}
            style={me.data.demo ? { opacity: 0.55 } : undefined}>
      <>
        {/* the web's settings search — the same keyword index, so the
            two pages find things by the same words */}
        <TextInput style={s.search} placeholder="Search settings…"
                   placeholderTextColor={C.mut} autoCapitalize="none"
                   value={filter} onChangeText={setFilter} />
        {owner && (hits.conn || hits.tokens || hits.move) && (
          <SectionHead>Connections</SectionHead>
        )}
        {owner && hits.conn && (<>
          <NavRow label="Bank connections"
                  sub={connSub
                    ?? "link, fix, sync, remove — on the Accounts tab"}
                  warn={connWarn}
                  href="/accounts" />
          <NavRow label="Where should your data come from?"
                  sub="providers compared · Plaid/MX keys · SimpleFIN"
                  href="/connect-hub" />
          <PlaidRecurringCard />
          {!me.data.demo && <EnrichCard />}
        </>)}
        {/* hosted included — the web renders script tokens everywhere */}
        {owner && hits.tokens && <ScriptTokensCard />}
        {owner && hits.move && !me.data.hosted && <ConnectionsBackupCard />}
        {owner && (hits.smtp || hits.daily)
          && <SectionHead>Email &amp; Push</SectionHead>}
        {owner && hits.smtp && !me.data.hosted && <SmtpCard />}
        {owner && hits.daily && (
          <NavRow label="Email & Push"
                  sub={schedSub
                    ?? "cadences, recipients, email, SMS + push channels"}
                  href="/notifications" />
        )}
        {/* the web's Settings → Integrations: what the instance tells
            other programs — webhooks out, the read doors in */}
        {owner && (hits.webhooks || hits.readtokens)
          && <SectionHead>Integrations</SectionHead>}
        {owner && hits.webhooks && !me.data.demo && <WebhooksCard />}
        {owner && hits.readtokens && !me.data.demo
          && <ScriptTokensCard scope="read" />}
        {owner && hits.readtokens && <EndpointsCard />}
        {hits.family && (<>
          <SectionHead>Users</SectionHead>
          <NavRow label="Family & invites"
                  sub="members, one-time invite links"
                  href="/security" />
        </>)}
        {/* the web's Settings → Preferences: how the app presents itself
            (theme is this phone's own; the home pages are the household's) */}
        {hits.prefs && (<>
          <SectionHead>Preferences</SectionHead>
          <ThemeCard />
          <HomePageCard />
        </>)}
        {(hits.feedback || (owner && hits.system))
          && <SectionHead>Data</SectionHead>}
        {/* Feedback is a member-visible toggle (the web and the server
            both allow it) — only System health below stays owner-only */}
        {hits.feedback && <FeedbackToggleCard />}
        {owner && hits.system && !me.data.hosted && (
          <NavRow label="System health"
                  sub="doctor checks, collectors" href="/doctor" />
        )}
        {hits.rules && (<>
          <SectionHead>Categorization</SectionHead>
          <NavRow label="Rules"
                  sub="learned rules, rename a custom category"
                  href="/rules" />
        </>)}
        {hits.merchants && (<>
          {/* its own section, like the web's Settings → Merchants */}
          <SectionHead>Merchants</SectionHead>
          <NavRow label="Merchants"
                  sub="rename or merge payees, details and where they are"
                  href="/merchants" />
          <LogosToggleCard />
        </>)}
        {owner && hits.llm && (<>
          <SectionHead>AI</SectionHead>
          <LlmCard hosted={!!me.data.hosted} />
        </>)}
        {owner && hits.setup && (<>
          <SectionHead>Wizards</SectionHead>
          <WizardsCard />
        </>)}
        {owner && hits.billing && AccountCard && (<>
          <SectionHead>{ext.account!.label}</SectionHead>
          {ext.settingsCards.map((Card, i) => <Card key={i} />)}
          <AccountCard />
        </>)}
        {owner && me.data.hosted && hits.support && (<>
          <SectionHead>Support</SectionHead>
          <SupportAccessCard owner />
        </>)}
        {hits.security && (<>
          <SectionHead>Security</SectionHead>
          <NavRow label="Security & household"
                  sub={me.data.totp_enabled
                    ? "2FA on · password, sessions, delete account"
                    : "no 2FA — password, sessions, delete account"}
                  warn={!me.data.totp_enabled}
                  href="/security" />
        </>)}
        {filter.trim() !== ""
          && !Object.values(hits).some(Boolean) && (
          <Text style={s.hint}>
            Nothing matches “{filter.trim()}” — try “smtp”, “2fa”,
            “export”.
          </Text>
        )}
      </>
      </View>
      )}
      <Pressable style={[s.signOut, revoke.isPending && { opacity: 0.5 }]}
                 disabled={revoke.isPending} onPress={confirmSignOut}>
        <Text style={s.signOutText}>
          {revoke.isPending ? "Signing out…" : "Sign out on this device"}
        </Text>
      </Pressable>
      <Text style={s.hint}>
        Passkey enrollment is managed on the web app.
      </Text>
    </ScrollView>
  );
}

// ---- SMTP (self-host) — where the daily verdict and alerts go OUT
// through; blank password keeps the stored one ----
function SmtpCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const d = settings.data as (Record<string, unknown> | undefined);
  const [seeded, setSeeded] = useState(false);
  const [host, setHost] = useState("");
  const [port, setPort] = useState("587");
  const [user, setUser] = useState("");
  const [pw, setPw] = useState("");
  const [from, setFrom] = useState("");
  const [starttls, setStarttls] = useState(true);
  const [msg, setMsg] = useState<string | null>(null);
  if (d && !seeded) {
    setHost(String(d.smtp_host ?? ""));
    setPort(String(d.smtp_port ?? 587));
    setUser(String(d.smtp_user ?? ""));
    setFrom(String(d.smtp_from ?? ""));
    setStarttls(d.smtp_starttls !== false);
    setSeeded(true);
  }
  const save = useMutation({
    mutationFn: () => saveSettings(qc, client!, {
      smtp_host: host.trim(), smtp_port: port.trim(),
      smtp_user: user.trim(), smtp_from: from.trim(),
      smtp_starttls: starttls,
      ...(pw.trim() ? { smtp_password: pw.trim() } : {}) }),
    onSuccess: () => {
      setPw(""); setMsg("Email delivery settings saved.");
      qc.invalidateQueries({ queryKey: ["doctor"] });
    },
    onError: (e) => setMsg(errText(e)),
  });
  const test = useMutation({
    mutationFn: () => client!.smtpTest(),
    onSuccess: (r) => {
      // a partial failure must read as a failure, and a skipped
      // address must be named
      const bits = [`sent to ${r.sent_to.join(", ") || "nobody"} via ${
        r.via}${r.failed.length ? "" : " ✓"}`];
      for (const sk of r.skipped ?? [])
        bits.push(`${sk.email} — not sent: ${sk.reason}`);
      for (const f0 of r.failed)
        bits.push(`${f0.email} failed — ${f0.error}`);
      setMsg(bits.join("\n"));
    },
    onError: (e) => setMsg(errText(e)),
  });
  const pwSet = !!d?.smtp_password_set;
  const [smtpOpen, setSmtpOpen] = useState(false);
  return (
    <Card>
      <View style={{ flexDirection: "row", alignItems: "center" }}>
        <View style={{ flex: 1 }}>
          <H>Email delivery (SMTP)</H>
          {/* the web's collapsed summary IS the current-state readout */}
          {!smtpOpen && (
            <Text style={s.hintL}>
              {host.trim()
                ? `${host.trim()}:${port.trim() || "587"}`
                : "using the operator's OIKONOME_SMTP_* settings"}
            </Text>
          )}
        </View>
        <Text style={{ color: C.accent, fontSize: 13 }}
              onPress={() => setSmtpOpen((x) => !x)}>
          {smtpOpen ? "done" : "change"}
        </Text>
      </View>
      {smtpOpen && (<>
      <Text style={s.hintL}>
        Where the daily verdict, alerts, invites, and feedback emails go
        OUT through — any provider works (Proton, Fastmail, Gmail
        app-password…). Leaving the host empty falls back to the
        operator&apos;s OIKONOME_SMTP_* settings.
      </Text>
      <TextInput style={s.input} placeholder="smtp.fastmail.com"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={host} onChangeText={setHost} />
      <View style={{ flexDirection: "row", gap: 6 }}>
        <TextInput style={[s.input, { width: 80 }]} placeholder="587"
                   placeholderTextColor={C.mut} keyboardType="number-pad"
                   value={port} onChangeText={setPort} />
        <TextInput style={[s.input, { flex: 1 }]}
                   placeholder="username (you@example.com)"
                   placeholderTextColor={C.mut} autoCapitalize="none"
                   value={user} onChangeText={setUser} />
      </View>
      <TextInput style={s.input}
                 placeholder={pwSet
                   ? "•••••••• (one is set — type to replace)"
                   : "password / app password"}
                 placeholderTextColor={C.mut} secureTextEntry
                 value={pw} onChangeText={setPw} />
      <TextInput style={s.input}
                 placeholder="from address (oikonome@example.com)"
                 placeholderTextColor={C.mut} autoCapitalize="none"
                 value={from} onChangeText={setFrom} />
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8,
                     marginTop: 6 }}>
        <Text style={{ color: C.text, fontSize: 13, flex: 1 }}>
          Use STARTTLS{" "}
          <Text style={s.hintL2}>
            — turn off only for a plain relay on your own network
          </Text>
        </Text>
        <Switch value={starttls} onValueChange={setStarttls}
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
      {msg ? (
        <Text style={{ color: msg.includes("failed") ? C.bad : C.good,
                       fontSize: 12, marginTop: 4 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      ) : null}
      <View style={{ flexDirection: "row", gap: 8, marginTop: 6 }}>
        <Pressable style={[s.btn, save.isPending && { opacity: 0.5 }]}
                   disabled={save.isPending}
                   onPress={() => save.mutate()}>
          <Text style={s.btnText}>
            {save.isPending ? "saving…" : "Save SMTP settings"}
          </Text>
        </Pressable>
        <Pressable style={[s.btn, s.btnQuiet,
                     test.isPending && { opacity: 0.5 }]}
                   disabled={test.isPending}
                   onPress={() => test.mutate()}>
          <Text style={[s.btnText, { color: C.mut }]}>
            {test.isPending ? "sending…" : "Send test email"}
          </Text>
        </Pressable>
      </View>
      </>)}
    </Card>
  );
}

// ---- AI backends (optional) — OpenAI-compatible endpoints + which
// one handles each task; secrets stay write-only ----
type Backend = {
  id: string; name: string; url: string; model: string;
  // extra_body follows the api_key contract: the server reports only
  // presence (extra_body_set), "" on save keeps the stored JSON, and
  // an explicit null clears it
  vision_model?: string; api_key?: string;
  extra_body?: string | null;
  api_key_set?: boolean; extra_body_set?: boolean;
};
const LLM_TASKS: [string, string][] = [
  ["assistant", "Assistant"], ["categorize", "Categorization & tags"],
  ["vision", "Receipts & documents"]];

function LlmCard({ hosted }: { hosted: boolean }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const d = settings.data as (Record<string, unknown> | undefined);
  const [seeded, setSeeded] = useState(false);
  const [backends, setBackends] = useState<Backend[]>([]);
  const [roles, setRoles] = useState<Record<string, string>>({});
  const [taxOk, setTaxOk] = useState(false);
  const [bundledVision, setBundledVision] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  if (d && !seeded) {
    const rows = (d.llm_backends as Backend[] | null) ?? [];
    // a legacy single-endpoint config shows as one synthetic row
    setBackends(rows.length ? rows
      : d.llm_url
        ? [{ id: "legacy", name: "Custom endpoint",
             url: String(d.llm_url), model: String(d.llm_model ?? ""),
             api_key_set: !!d.llm_api_key_set }]
        : []);
    setRoles((d.llm_roles as Record<string, string> | null) ?? {});
    setTaxOk(!!d.taxdocs_allow_remote_llm);
    setBundledVision(String(d.llm_vision_model ?? ""));
    setSeeded(true);
  }
  const bundled = !!d?.llm_bundled;
  const bundledModel = (d?.llm_bundled as { model?: string } | undefined)
    ?.model ?? "";
  // the web's local/cloud tag — where the merchant names actually go
  const isLocal = (url: string) => {
    const h = (url.split("//")[1] ?? "").split(/[/:]/)[0].toLowerCase();
    return !!h && (h === "localhost" || h.endsWith(".local")
      || !h.includes(".")
      || /^(10\.|192\.168\.|127\.|172\.(1[6-9]|2\d|3[01])\.)/.test(h));
  };
  const effective = (d?.llm_effective ?? {}) as
    Record<string, { model?: string; url?: string } | undefined>;
  const save = useMutation({
    mutationFn: () => saveSettings(qc, client!, {
      llm_backends: backends.map(
        ({ api_key_set, extra_body_set, ...rest }) => rest),
      llm_roles: roles,
      taxdocs_allow_remote_llm: taxOk ? "1" : "",
      ...(bundled ? { llm_vision_model: bundledVision.trim() } : {}) }),
    onSuccess: () => {
      setMsg("LLM settings saved.");
      // the reply carries api_key_set / extra_body_set for what was just
      // stored; re-seed so the form shows presence instead of the typed
      // secret, and a second save doesn't resend it
      setSeeded(false);
    },
    onError: (e) => setMsg(errText(e)),
  });
  const patch = (id: string, p: Partial<Backend>) =>
    setBackends((bs) => bs.map((b) =>
      b.id === id ? { ...b, ...p } : b));
  return (
    <Card>
      <H>AI (optional)</H>
      <Text style={s.hintL}>
        Everything core works without AI. Add one or more
        OpenAI-compatible backends
        {hosted
          ? " — public APIs only (OpenAI, OpenRouter…); a model on "
            + "your home network is not reachable from this server"
          : " — local (Ollama, LM Studio…, no API key needed) or "
            + "cloud"}
        . They power the Assistant, categorize unknown merchants,
        summarize Amazon orders, tag recurring proposals, and — with a
        vision model — read receipts and paper documents.
      </Text>
      {bundled && (
        <>
          <Text style={{ color: C.text, fontSize: 13, marginTop: 6,
                         fontWeight: "700" }}>
            Bundled Ollama{" "}
            <Text style={s.hintL2}>
              {bundledModel || "(no model)"} · local · built-in ·
              managed by the install
            </Text>
          </Text>
          <Text style={s.hintL2}>
            Vision model — reads receipts and documents when the bundled
            backend handles them. `./oikonome.sh llm vision` pulls
            qwen2.5vl:3b. Empty turns it off.
          </Text>
          <TextInput style={s.input}
                     placeholder="vision model (qwen2.5vl:3b, empty = off)"
                     placeholderTextColor={C.mut} autoCapitalize="none"
                     value={bundledVision}
                     onChangeText={setBundledVision} />
        </>
      )}
      {backends.map((b) => editing === b.id ? (
        <View key={b.id} style={{ borderColor: C.border, borderWidth: 1,
                                  borderRadius: 8, padding: 8,
                                  marginTop: 6, gap: 6 }}>
          <TextInput style={s.input} placeholder="name"
                     placeholderTextColor={C.mut} value={b.name}
                     onChangeText={(v) => patch(b.id, { name: v })} />
          <TextInput style={s.input}
                     placeholder={hosted ? "https://api.openai.com/v1"
                       : "http://localhost:11434/v1"}
                     placeholderTextColor={C.mut} autoCapitalize="none"
                     value={b.url}
                     onChangeText={(v) => patch(b.id, { url: v })} />
          <TextInput style={s.input}
                     placeholder={hosted ? "gpt-4o-mini" : "llama3.1:8b"}
                     placeholderTextColor={C.mut} autoCapitalize="none"
                     value={b.model}
                     onChangeText={(v) => patch(b.id, { model: v })} />
          <Text style={s.hintL2}>
            Vision model (optional) — reads receipt images/PDFs
            {hosted
              ? "; use a vision-capable model from the same API"
              : "; bundled Ollama: `./oikonome.sh llm vision` pulls "
                + "qwen2.5vl:3b"}
          </Text>
          <TextInput style={s.input}
                     placeholder={hosted ? "gpt-4o-mini" : "qwen2.5vl:3b"}
                     placeholderTextColor={C.mut} autoCapitalize="none"
                     value={b.vision_model ?? ""}
                     onChangeText={(v) =>
                       patch(b.id, { vision_model: v })} />
          <TextInput style={s.input}
                     placeholder={b.api_key_set
                       ? "•••••••• (one is set — type to replace)"
                       : hosted ? "API key (required for most clouds)"
                       : "API key (optional — cloud only)"}
                     placeholderTextColor={C.mut} secureTextEntry
                     value={b.api_key ?? ""}
                     onChangeText={(v) => patch(b.id, { api_key: v })} />
          {b.api_key_set && (
            <Text style={{ color: C.warn, fontSize: 12 }}
                  onPress={() => patch(b.id, { api_key: "" })}>
              {/* the explicit empty string is the only "clear the
                  stored key" — a blank field means keep */}
              Remove the stored API key
            </Text>
          )}
          <TextInput style={[s.input, { fontFamily: "monospace",
                       fontSize: 12, minHeight: 52,
                       textAlignVertical: "top" }]}
                     placeholder={b.extra_body_set
                       ? "JSON options set — leave blank to keep them"
                       : ('extra request body (advanced JSON, '
                          + 'merged into every call)')}
                     placeholderTextColor={C.mut} multiline
                     autoCapitalize="none" autoCorrect={false}
                     value={b.extra_body ?? ""}
                     onChangeText={(v) =>
                       patch(b.id, { extra_body: v })} />
          {b.extra_body_set && (
            <Text style={{ color: C.warn, fontSize: 12 }}
                  onPress={() => patch(b.id, { extra_body: null })}>
              {/* explicit null is the only "clear the stored JSON" —
                  a blank field means keep, like the key above */}
              Remove the stored JSON options
            </Text>
          )}
          <View style={{ flexDirection: "row", gap: 8 }}>
            {/* the web's required-field gate: a backend without a URL
                and model is a row that can only fail later */}
            <Pressable style={[s.btn,
                         (!b.url.trim() || !b.model.trim())
                           && { opacity: 0.4 }]}
                       disabled={!b.url.trim() || !b.model.trim()}
                       onPress={() => setEditing(null)}>
              <Text style={s.btnText}>apply</Text>
            </Pressable>
            <Pressable style={[s.btn, s.btnQuiet]}
                       onPress={() => { setBackends((bs) =>
                         bs.filter((x) => x.id !== b.id));
                         setEditing(null); }}>
              <Text style={[s.btnText, { color: C.bad }]}>remove</Text>
            </Pressable>
          </View>
        </View>
      ) : (
        <View key={b.id} style={{ flexDirection: "row",
                 alignItems: "center", gap: 8,
                 borderTopColor: C.border,
                 borderTopWidth: StyleSheet.hairlineWidth,
                 paddingVertical: 6 }}>
          <View style={{ flex: 1 }}>
            <Text style={{ color: C.text, fontSize: 13,
                           fontWeight: "600" }}>{b.name}</Text>
            <Text style={s.hintL2} numberOfLines={1}>
              {b.model}
              {b.vision_model ? ` · vision ${b.vision_model}` : ""}
              {" · "}
              <Text style={{ color: isLocal(b.url) ? C.good : C.warn }}>
                {isLocal(b.url) ? "local" : "cloud"}
              </Text>
              {" · "}{b.url}
            </Text>
          </View>
          <Text style={{ color: C.accent, fontSize: 12, padding: 4 }}
                onPress={() => setEditing(b.id)}>edit</Text>
        </View>
      ))}
      <Text style={{ color: C.accent, fontSize: 12, marginTop: 6 }}
            onPress={() => {
              const id = Math.random().toString(36).slice(2, 10);
              setBackends((bs) => [...bs,
                { id, name: "", url: "", model: "" }]);
              setEditing(id);
            }}>
        + Add backend
      </Text>
      <Text style={s.hintL2}>
        Free options that work here: Google Gemini (AI Studio free
        tier), Groq, and OpenRouter&apos;s free-routed models. One honest
        caveat: free tiers generally reserve the right to use what you
        send them — and what this sends is merchant names from your
        transactions. Read the provider&apos;s data policy first.
        Nothing is saved until you hit Save AI settings.
      </Text>
      {/* the per-household overlay needs no operator setup — it trains
          from the household's own corrections whether or not a shipped
          model file is mounted, so this line renders unconditionally
          (web wording, copied) */}
      <Text style={s.hintL2}>
        Each night this instance also retrains a small per-household
        model from your accumulated category corrections — it
        categorizes new lookalike merchants on this machine, before any
        backend below is consulted, and your data never leaves the
        instance.
      </Text>
      {d?.categorizer_active ? (
        <Text style={s.hintL2}>
          A built-in trained classifier also categorizes merchants on
          this machine, after the per-household model — the backend
          routed to Categorization only handles what both abstain on,
          plus proposal tags and Amazon summaries.
        </Text>
      ) : null}
      <Text style={[s.hintL, { marginTop: 8, color: C.text,
                               fontWeight: "700" }]}>
        What uses what
      </Text>
      {LLM_TASKS.map(([role, label]) => (
        <View key={role} style={{ marginTop: 4 }}>
          <Text style={s.hintL2}>{label}</Text>
          <View style={{ flexDirection: "row", flexWrap: "wrap",
                         gap: 6, marginTop: 2 }}>
            <Pressable style={[s.chip, !roles[role] && s.chipOn]}
                       onPress={() => setRoles((r) => {
                         const n = { ...r }; delete n[role]; return n;
                       })}>
              <Text style={{ color: !roles[role] ? C.text : C.mut,
                             fontSize: 11 }}>
                default{effective[role]
                  ? ` (${effective[role]!.model
                       || effective[role]!.url})` : " (off)"}
              </Text>
            </Pressable>
            {bundled && (
              <Pressable style={[s.chip,
                           roles[role] === "bundled" && s.chipOn]}
                         onPress={() => setRoles((r) =>
                           ({ ...r, [role]: "bundled" }))}>
                <Text style={{ color: roles[role] === "bundled"
                                 ? C.text : C.mut, fontSize: 11 }}>
                  Bundled Ollama
                </Text>
              </Pressable>
            )}
            {backends.map((b) => (
              <Pressable key={b.id}
                         style={[s.chip, roles[role] === b.id && s.chipOn]}
                         onPress={() => setRoles((r) =>
                           ({ ...r, [role]: b.id }))}>
                <Text style={{ color: roles[role] === b.id
                                 ? C.text : C.mut, fontSize: 11 }}>
                  {b.name || "backend"}
                </Text>
              </Pressable>
            ))}
          </View>
        </View>
      ))}
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8,
                     marginTop: 8 }}>
        <Text style={{ color: C.text, fontSize: 12, flex: 1,
                       lineHeight: 17 }}>
          Allow sensitive documents to be sent to my remote model{" "}
          <Text style={s.hintL2}>
            — off by default. W-2/1040 imports send an image of the whole
            document, including your Social Security number; a photographed
            check sends its face, including the full routing and account
            number.
          </Text>
        </Text>
        <Switch value={taxOk} onValueChange={setTaxOk}
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
      {msg ? (
        <Text style={{ color: msg.includes("saved") ? C.good : C.bad,
                       fontSize: 12, marginTop: 4 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      ) : null}
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 8 },
                   (save.isPending || editing !== null)
                     && { opacity: 0.5 }]}
                 disabled={save.isPending || editing !== null}
                 onPress={() => save.mutate()}>
        <Text style={s.btnText}>
          {save.isPending ? "saving…" : "Save AI settings"}
        </Text>
      </Pressable>
    </Card>
  );
}

// passphrase-sealed .oikx credentials bundle — move to a fresh
// environment with banks/email/LLM still wired
function ConnectionsBackupCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  // the bundle's own passphrases — secrets being SET, not re-auth; the
  // account proof is the app-wide sheet
  const [pass, setPass] = useState("");
  const [confirm, setConfirm] = useState("");
  const [importPass, setImportPass] = useState("");
  const [file, setFile] = useState<PickedFile | null>(null);
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(
    null);
  const fail = (e: unknown) => setMsg({ text: stepUpMsg(e), bad: true });
  const exp = useMutation({
    mutationFn: () => client!.connectionsExport(pass),
    onSuccess: () => {
      setPass(""); setConfirm("");
      setMsg({ text: "Bundle downloaded. Keep it and its passphrase "
                     + "safe." });
    },
    onError: fail,
  });
  const imp = useMutation({
    mutationFn: () => client!.connectionsImport(file!, importPass),
    onSuccess: (r) => {
      setFile(null); setImportPass("");
      setMsg({ text: `Imported ${r.items} bank link${
        r.items === 1 ? "" : "s"}; config restored: ${
        r.config.join(", ") || "none"}. Run a sync to pull accounts.` });
      // the bundle restores provider config and bank links — refresh
      // what it touches instead of refetching every query in the app
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["connections"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
    onError: fail,
  });
  const canExport = pass.length >= 8 && pass === confirm;
  // this door hands over every credential the instance holds, so the
  // server wants fresh proof — a native confirm sits in front of it
  const askExport = () => Alert.alert("Download the connections bundle?",
    "Every provider key and bank link, sealed with your passphrase. "
    + "You'll confirm it's you first.",
    [{ text: "Download", onPress: () => exp.mutate() },
     { text: "Cancel", style: "cancel" }]);
  return (
    <Card>
      <H>Move to a fresh environment</H>
      <Text style={s.hintL}>
        A passphrase-sealed bundle of your instance credentials —
        provider keys, LLM endpoint, SMTP, and every live bank link.
        Unlike the database dump it survives a different install.
        Accounts, history and budgets ride the export ZIP; one sync
        after import repopulates accounts.
      </Text>
      <Text style={[s.hintL, { marginTop: 8, color: C.text,
                               fontWeight: "700" }]}>Export</Text>
      <TextInput style={s.input}
                 placeholder="passphrase (8+ chars — needed on the other side)"
                 placeholderTextColor={C.mut} secureTextEntry
                 value={pass} onChangeText={setPass} />
      <TextInput style={s.input} placeholder="confirm passphrase"
                 placeholderTextColor={C.mut} secureTextEntry
                 value={confirm} onChangeText={setConfirm} />
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 6 },
                   (!canExport || exp.isPending) && { opacity: 0.5 }]}
                 disabled={!canExport || exp.isPending}
                 onPress={askExport}>
        <Text style={s.btnText}>
          {exp.isPending ? "sealing…" : "Download connections bundle"}
        </Text>
      </Pressable>
      <Text style={[s.hintL, { marginTop: 8, color: C.text,
                               fontWeight: "700" }]}>Import</Text>
      <Pressable style={[s.btn, s.btnQuiet,
                   { alignSelf: "flex-start", marginTop: 4 }]}
                 onPress={async () => {
                   const r0 = await DocumentPicker.getDocumentAsync(
                     { copyToCacheDirectory: true });
                   if (!r0.canceled && r0.assets[0])
                     setFile({ uri: r0.assets[0].uri,
                               name: r0.assets[0].name });
                 }}>
        <Text style={[s.btnText, { color: C.mut }]}>
          {file ? file.name : "choose bundle file (.oikx)"}
        </Text>
      </Pressable>
      <TextInput style={s.input} placeholder="passphrase"
                 placeholderTextColor={C.mut} secureTextEntry
                 value={importPass} onChangeText={setImportPass} />
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 6 },
                   (!file || !importPass || imp.isPending)
                     && { opacity: 0.5 }]}
                 disabled={!file || !importPass || imp.isPending}
                 onPress={() => imp.mutate()}>
        <Text style={s.btnText}>
          {imp.isPending ? "importing…" : "Import connections"}
        </Text>
      </Pressable>
      {msg && (
        <Text style={{ color: msg.bad ? C.bad : C.good, fontSize: 12,
                       marginTop: 6 }}
              onPress={() => setMsg(null)}>{msg.text}</Text>
      )}
    </Card>
  );
}

// re-enter the guided walkthroughs
function WizardsCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const router = useRouter();
  // "continue" only means something while a step is left: a finished
  // wizard resumes on its last step and has nothing to continue
  const ob = useQuery({ queryKey: ["onboarding"],
    queryFn: () => client!.onboarding(), enabled: !!client });
  return (
    <Card>
      {/* the web card's name and order: Guided setup, with its
          nothing-is-deleted promise up front */}
      <H>Guided setup</H>
      <Text style={s.hintL}>
        Walkthroughs you can re-run any time — nothing is deleted, and
        you can skip out at any step.
      </Text>
      {ob.data && !ob.data.wizard_done && (
        <Text style={{ color: C.accent, fontSize: 13, paddingVertical: 4 }}
              onPress={() => router.push("/welcome" as never)}>
          Finish setup — continue where you left off ›
        </Text>
      )}
      <Text style={{ color: C.accent, fontSize: 13, paddingVertical: 4 }}
            onPress={() => Alert.alert("Onboarding wizard",
              "The setup checklist returns to Today until its steps "
              + "are done again. Nothing is deleted.",
              [{ text: "Restart",
                 onPress: () => saveSettings(qc, client!, {
                   wizard_done: false,
                   wizard_steps: { connect: null, sync: null,
                     import: null, bills: null, budgets: null,
                     email: null, finish: null } })
                   .then(() => qc.invalidateQueries(
                     { queryKey: ["onboarding"] }))
                   .catch((e) =>
                     Alert.alert("Couldn't restart", errText(e))) },
               { text: "Cancel", style: "cancel" }])}>
        Onboarding wizard — restart the Today checklist ›
      </Text>
      <Text style={{ color: C.accent, fontSize: 13, paddingVertical: 4 }}
            onPress={() => router.push("/business-wizard" as never)}>
        Business wizard ›
      </Text>
      <Text style={{ color: C.accent, fontSize: 13, paddingVertical: 4 }}
            onPress={() =>
              router.push("/retire?wizard=1" as never)}>
        Retirement wizard ›
      </Text>
    </Card>
  );
}

// instance feedback on/off (self-host owner)
function EnrichCard() {
  // the web's "Describe imported history with Plaid" card — billed per
  // row, so a button and a monthly cap, never automatic
  const { client } = useSession();
  const viewer = useViewer();
  // Enrich is billed per row and the server takes it from the OWNER
  // only — a member's button would 403, as the web's owner gate knows
  const owner = useOwner();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["enrich-status"],
    queryFn: () => client!.enrichStatus(), enabled: !!client && owner });
  const [last, setLast] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: () => client!.enrichRun(100),
    onSuccess: (r) => {
      setLast(r.error === "already running" ? "An Enrich run is already in progress."
        : r.error ? `Plaid said: ${r.error}`
        : r.cap_hit ? "This month's cap is used up."
        : `Sent ${r.sent}, described ${r.enriched}.`);
      qc.invalidateQueries({ queryKey: ["enrich-status"] });
      // the ledger only changed if a row actually got described
      if (r.enriched > 0)
        qc.invalidateQueries({ queryKey: ["transactions"] });
    },
    onError: (e) => setLast(errText(e)),
  });
  const d = q.data;
  // the cap is the only thing on this card that is a setting rather than an
  // action; null draft = untouched, so the box follows the server until the
  // moment someone types in it (the web's EnrichCard, same rule)
  const [capDraft, setCapDraft] = useState<string | null>(null);
  const capValue = capDraft ?? (d ? String(d.cap) : "");
  const capNum = Number.parseInt(capValue, 10);
  const capOk = Number.isFinite(capNum) && capNum >= 0
    && !!d && capNum !== d.cap;
  const saveCap = useMutation({
    mutationFn: () => saveSettings(qc, client!, { plaid_enrich_cap: capNum }),
    onSuccess: () => {
      // the draft falls back to the cached cap the moment it clears, so
      // the cache must already hold the new one or the box snaps back
      qc.setQueryData<typeof d>(["enrich-status"], (o) => o && {
        ...o, cap: capNum, remaining: Math.max(0, capNum - o.used) });
      setCapDraft(null); setLast("Monthly cap saved.");
    },
    onError: (e) => setLast(errText(e)),
  });
  if (!owner) return null;   // the run and its numbers are the owner's
  return (
    <Card>
      <Text style={{ color: C.text, fontSize: 14, fontWeight: "600" }}>
        Describe imported history with Plaid
      </Text>
      <Text style={{ color: C.mut, fontSize: 12, marginTop: 2 }}>
        Rows from files and other providers get Plaid's merchant, logo,
        location and category — billed per row, so only on request, newest
        first, inside a monthly cap.
      </Text>
      {d ? (
        <Text style={{ color: C.mut, fontSize: 12, marginTop: 6 }}>
          {d.candidates} still undescribed · {d.used} of {d.cap} used this month
          {!d.plaid_configured ? " · Plaid is not configured" : ""}
        </Text>
      ) : null}
      {!viewer && (
        <Pressable style={{ backgroundColor: C.accent, borderRadius: 8,
                            paddingHorizontal: 14, paddingVertical: 8,
                            alignSelf: "flex-start", marginTop: 8,
                            opacity: (!d || !d.plaid_configured || d.candidates === 0
                                      || d.remaining === 0 || run.isPending) ? 0.5 : 1 }}
                   disabled={!d || !d.plaid_configured || d.candidates === 0
                             || d.remaining === 0 || run.isPending}
                   onPress={() => run.mutate()}>
          <Text style={{ color: C.text, fontWeight: "600" }}>
            {run.isPending ? "Sending…" : "Describe the next 100 rows"}
          </Text>
        </Pressable>
      )}
      {!viewer && (
        <View style={{ flexDirection: "row", alignItems: "center", gap: 8,
                       marginTop: 8 }}>
          <Text style={{ color: C.mut, fontSize: 12 }}>
            Monthly cap (rows)
          </Text>
          <TextInput style={[s.input, { marginTop: 0, minWidth: 88 }]}
                     keyboardType="number-pad"
                     value={capValue}
                     onChangeText={setCapDraft}
                     placeholderTextColor={C.mut} />
          <Pressable style={{ backgroundColor: C.accent, borderRadius: 8,
                              paddingHorizontal: 12, paddingVertical: 6,
                              opacity: !capOk || saveCap.isPending ? 0.5 : 1 }}
                     disabled={!capOk || saveCap.isPending}
                     onPress={() => saveCap.mutate()}>
            <Text style={{ color: C.text, fontSize: 13, fontWeight: "600" }}>
              {saveCap.isPending ? "saving…" : "Save"}
            </Text>
          </Pressable>
        </View>
      )}
      {last ? <Text style={{ color: C.mut, fontSize: 12, marginTop: 6 }}>{last}</Text> : null}
    </Card>
  );
}


// A Switch bound to cached settings has to move the moment it is
// tapped: flip the cache first, let the reply (the full settings view)
// settle it, and put the old value back only if the server refused.
function useSettingToggle(key: string, alsoMe?: string) {
  const { client } = useSession();
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (v: boolean) => saveSettings(qc, client!, { [key]: v }),
    onMutate: async (v) => {
      await qc.cancelQueries({ queryKey: ["settings"] });
      const prev = qc.getQueryData<SettingsView>(["settings"]);
      const prevMe = alsoMe ? qc.getQueryData<Me>(["me"]) : undefined;
      patchQuery<SettingsView>(qc, ["settings"], { [key]: v });
      if (alsoMe) patchQuery<Me>(qc, ["me"], { [alsoMe]: v });
      return { prev, prevMe };
    },
    onError: (e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["settings"], ctx.prev);
      if (alsoMe && ctx?.prevMe) qc.setQueryData(["me"], ctx.prevMe);
      Alert.alert("Couldn't save", errText(e));
    },
  });
}

function PlaidRecurringCard() {
  // the web's "Cross-check bills against Plaid's recurring streams" box
  const { client } = useSession();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const on = (settings.data as Record<string, unknown> | undefined)
    ?.plaid_recurring !== false;
  const toggle = useSettingToggle("plaid_recurring");
  return (
    <Card>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 10 }}>
        <View style={{ flex: 1 }}>
          <Text style={{ color: C.text, fontSize: 14 }}>
            Cross-check bills against Plaid's recurring streams
          </Text>
          <Text style={{ color: C.mut, fontSize: 12 }}>
            Plaid's own recurring detection, offered nightly as bill
            proposals beside ours — never applied without you
          </Text>
        </View>
        <Switch value={on} onValueChange={(v) => toggle.mutate(v)}
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
    </Card>
  );
}


function LogosToggleCard() {
  // the web's "Show merchant logos" checkbox (Settings → Merchants)
  const { client } = useSession();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const on = (settings.data as Record<string, unknown> | undefined)
    ?.merchant_logos !== false;
  // every avatar reads the flag from ["me"], so that copy flips too
  const toggle = useSettingToggle("merchant_logos", "merchant_logos");
  return (
    <Card>
      <View style={{ flexDirection: "row", alignItems: "center",
                     gap: 10 }}>
        <View style={{ flex: 1 }}>
          <Text style={{ color: C.text, fontSize: 14 }}>
            Show merchant logos
          </Text>
          <Text style={{ color: C.mut, fontSize: 12 }}>
            on the ledger, merchants, spending and accounts; off for a
            plainer look (the daily email never carries images)
          </Text>
        </View>
        <Switch value={on} onValueChange={(v) => toggle.mutate(v)}
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
    </Card>
  );
}


function FeedbackToggleCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const on = (settings.data as Record<string, unknown> | undefined)
    ?.feedback_enabled !== false;
  const toggle = useSettingToggle("feedback_enabled");
  return (
    <Card>
      <View style={{ flexDirection: "row", alignItems: "center",
                     gap: 10 }}>
        <Text style={{ color: C.text, fontSize: 14, flex: 1 }}>
          Feedback &amp; bug reports enabled on this instance
        </Text>
        <Switch value={on}
                onValueChange={(v) => toggle.mutate(v, {
                  // the Feedback screen asks /api/testing, not settings
                  onSuccess: () =>
                    qc.invalidateQueries({ queryKey: ["testing"] }) })}
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
    </Card>
  );
}

// the shared download button lives in components/; other screens still
// import it through here, so keep the old path working
export { DownloadButton } from "../components/download-button";

// ---- theme — resolved at bundle load, so a change applies on the
// next launch (every screen's styles capture the palette at import) ----
function MyEmailCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
                        enabled: !!client });
  const d = me.data?.daily_email;
  const [err, setErr] = useState<string | null>(null);
  const set = useMutation({
    mutationFn: (muted: boolean) => client!.meEmail(muted),
    onSuccess: (r) => {
      setErr(null);
      // the label reads me.daily_email.muted — the reply carries it, so
      // the button flips now rather than after a refetch
      qc.setQueryData<Me>(["me"], (o) => o?.daily_email && {
        ...o, daily_email: { ...o.daily_email, muted: r.muted } });
    },
    onError: (e) => setErr(errText(e)),
  });
  if (!d) return null;
  const hourLabel = (h: number) =>
    h === 0 ? "12am" : h < 12 ? `${h}am` : h === 12 ? "12pm" : `${h - 12}pm`;
  const receiving = d.on && !d.muted;
  return (
    <Card>
      <H>Daily email</H>
      <Text style={s.hintL}>
        The household's verdict email goes out at {hourLabel(d.hour)} as{" "}
        {d.summary ? "the verdict-only card" : "the full report"} — the
        owner sets that. Whether it reaches your inbox is yours:
      </Text>
      {!d.on && (
        <Text style={s.hintL}>The owner has the daily email turned off for
          the household right now.</Text>
      )}
      <Text style={[s.hintL, { color: C.text, marginTop: 6 }]}>
        {receiving ? "You receive it."
          : d.on ? "You've turned it off for yourself." : "Off."}
      </Text>
      {d.on && (
        <Pressable style={[s.testBtn, set.isPending && { opacity: 0.5 }]}
                   disabled={set.isPending}
                   onPress={() => set.mutate(!d.muted)}>
          <Text style={{ color: C.text, fontWeight: "600" }}>
            {d.muted ? "Turn on for me" : "Turn off for me"}
          </Text>
        </Pressable>
      )}
      {err && <Text style={[s.hint, { color: C.warn }]}>{err}</Text>}
    </Card>
  );
}

function ThemeCard() {
  const { client } = useSession();
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
    enabled: !!client, staleTime: 5 * 60_000 });
  const [mode, setMode] = useState<ThemeMode>(getThemeMode());
  const [pending, setPending] = useState(themeRestartPending());
  // the web rule: the owner's theme follows the ACCOUNT (settings.theme,
  // reconciled across devices); viewers keep a per-device choice —
  // their POST would 403 and the reconcile would revert them anyway
  const persist = me.data?.role === "owner" && !me.data.demo;
  return (
    <Card>
      <H>Appearance</H>
      <View style={{ flexDirection: "row", gap: 8, marginTop: 4 }}>
        {(["auto", "dark", "light"] as ThemeMode[]).map((m0) => (
          <Pressable key={m0} style={[s.chip, mode === m0 && s.chipOn]}
                     onPress={() => { setThemeMode(m0); setMode(m0);
                                      setPending(themeRestartPending());
                                      if (persist)
                                        client!.settingsSave(
                                          { theme: m0 }).catch(() => {});
                                    }}>
            <Text style={{ color: mode === m0 ? C.text : C.mut,
                           fontSize: 13 }}>
              {m0 === "auto" ? "match system" : m0}
            </Text>
          </Pressable>
        ))}
      </View>
      <Text style={s.hintL}>
        {pending
          ? "Close and reopen the app to apply."
          : "Auto follows the system setting at each launch."}
        {persist ? " Synced to your account, like the web." : ""}
      </Text>
    </Card>
  );
}

// ---- support access (hosted) ----
function SupportAccessCard({ owner }: { owner: boolean }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["support-access"],
    queryFn: () => client!.supportAccess(), enabled: !!client });
  const [hours, setHours] = useState(24);
  const [reason, setReason] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const grant = useMutation({
    mutationFn: () => client!.supportAccessGrant(hours, reason.trim()),
    onSuccess: () => {
      // flip the card now; the server's exact expiry lands on the refetch
      patchQuery(qc, ["support-access"], { granted: true,
        expires_at: new Date(Date.now() + hours * 3600_000).toISOString(),
        reason: reason.trim() || null });
      setReason(""); setMsg(null);
      qc.invalidateQueries({ queryKey: ["support-access"] });
    },
    onError: (e) => setMsg(stepUpMsg(e)),
  });
  const revoke = useMutation({
    mutationFn: () => client!.supportAccessRevoke(),
    onSuccess: () => patchQuery(qc, ["support-access"],
      { granted: false, expires_at: null, reason: null }),
    onError: (e) => setMsg(errText(e)),
  });
  const g = q.data;
  if (!g) return null;
  return (
    <Card>
      <H>Support access</H>
      <Text style={s.hintL}>
        Oikonome support can never see your data unless you grant it. A
        grant is time-boxed, revocable, and every access it allows is
        logged.
      </Text>
      {g.granted ? (
        <>
          <Text style={{ color: C.good, fontSize: 13, marginTop: 4 }}>
            Support access is on until{" "}
            {g.expires_at ? new Date(g.expires_at).toLocaleString() : "…"}.
            {g.reason ? `  Reason: ${g.reason}` : ""}
          </Text>
          {owner && (
            <Pressable style={[s.btn, s.btnQuiet,
                         { alignSelf: "flex-start", marginTop: 6 },
                         revoke.isPending && { opacity: 0.5 }]}
                       disabled={revoke.isPending}
                       onPress={() => revoke.mutate()}>
              <Text style={[s.btnText, { color: C.warn }]}>
                {revoke.isPending ? "revoking…" : "revoke now"}
              </Text>
            </Pressable>
          )}
          {msg ? (
            <Text style={{ color: C.bad, fontSize: 12 }}
                  onPress={() => setMsg(null)}>{msg}</Text>
          ) : null}
        </>
      ) : !owner ? (
        <Text style={s.hintL}>
          Only the account owner can grant support access.
        </Text>
      ) : (
        <>
          <View style={{ flexDirection: "row", gap: 6, marginTop: 6 }}>
            {([[1, "1 hour"], [24, "24 hours"], [72, "3 days"],
               [168, "7 days"]] as [number, string][]).map(([h, l]) => (
              <Pressable key={h} style={[s.chip, hours === h && s.chipOn]}
                         onPress={() => setHours(h)}>
                <Text style={{ color: hours === h ? C.text : C.mut,
                               fontSize: 11 }}>{l}</Text>
              </Pressable>
            ))}
          </View>
          <TextInput style={s.input} placeholder="reason (optional)"
                     placeholderTextColor={C.mut}
                     value={reason} onChangeText={setReason} />
          {msg ? (
            <Text style={{ color: C.bad, fontSize: 12 }}
                  onPress={() => setMsg(null)}>{msg}</Text>
          ) : null}
          <Pressable style={[s.btn, { alignSelf: "flex-start",
                       marginTop: 6 },
                       grant.isPending && { opacity: 0.5 }]}
                     disabled={grant.isPending}
                     onPress={() => grant.mutate()}>
            <Text style={s.btnText}>grant support access</Text>
          </Pressable>
        </>
      )}
    </Card>
  );
}

// ---- script tokens (self-host collectors) ----
// One card, two scopes (the web's shape): Connections renders the push
// half (a collector's credential), Integrations the read half (a scraper's,
// a sensor's, an MCP server's).
function ScriptTokensCard({ scope = "push" }: { scope?: TokenScope }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["tokens"],
    queryFn: () => client!.tokens(), enabled: !!client });
  const [name, setName] = useState("");
  const [fresh, setFresh] =
    useState<{ name: string; token: string } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const read = scope === "read";
  // a token is a durable grant, so minting/revoking one is elevation-
  // gated — the same sheet the support-access grant raises
  const mint = useMutation({
    mutationFn: () => client!.tokenMint(name.trim(), scope),
    onSuccess: (r) => {
      // the plaintext appears ONLY here — it won't be shown again
      setFresh({ name: r.name, token: r.token });
      setName(""); setMsg(null);
      // show the row now; the refetch only firms up created_at
      qc.setQueryData<typeof q.data>(["tokens"], (o) => o && { ...o,
        tokens: [...o.tokens, { id: r.id, name: r.name,
          created_at: new Date().toISOString(), last_used_at: null,
          revoked_at: null, scope: r.scope ?? scope }] });
      qc.invalidateQueries({ queryKey: ["tokens"] });
    },
    onError: (e) => setMsg(stepUpMsg(e)),
  });
  const revoke = useMutation({
    mutationFn: (id: string) => client!.tokenRevoke(id),
    onSuccess: (_r, id) => removeFromList(qc, ["tokens"], "tokens",
      (t: { id: string }) => t.id === id),
    onError: (e) => setMsg(stepUpMsg(e)),
  });
  // an older row carries no scope and was minted when push was the only kind
  const rows = (q.data?.tokens ?? [])
    .filter((t) => !t.revoked_at && (t.scope ?? "push") === scope);
  return (
    <Card>
      <H>{read ? "Integration tokens" : "Script tokens"}</H>
      <Text style={s.hintL}>
        {read
          ? "Read-only credentials for the programs that watch your "
            + "money: a Prometheus scrape, a Home Assistant sensor, the "
            + "local MCP server. A read token can read the integrations "
            + "endpoints — nothing else: it can't import a row, change a "
            + "setting or see a credential."
          : "Credentials for your own collector scripts. A token can push "
            + "data through the import endpoints — nothing else: it can't "
            + "read your ledger or change settings."}
      </Text>
      {fresh && (
        <View style={{ backgroundColor: C.bg, borderColor: C.good,
                       borderWidth: 1, borderRadius: 8, padding: 8,
                       marginTop: 6, gap: 4 }}>
          <Text style={s.hintL}>
            {fresh.name} — copy this token now; it won&apos;t be shown
            again:
          </Text>
          <Text style={{ color: C.text, fontFamily: "monospace",
                         fontSize: 12 }} selectable>
            {fresh.token}
          </Text>
          <Pressable style={[s.btn, { alignSelf: "flex-start" }]}
                     onPress={async () => {
                       await Clipboard.setStringAsync(fresh.token);
                       setFresh(null);
                     }}>
            <Text style={s.btnText}>Copy &amp; dismiss</Text>
          </Pressable>
        </View>
      )}
      {rows.map((t) => (
        <View key={t.id} style={{ flexDirection: "row",
                 alignItems: "center", gap: 8,
                 borderTopColor: C.border,
                 borderTopWidth: StyleSheet.hairlineWidth,
                 paddingVertical: 6 }}>
          <View style={{ flex: 1 }}>
            <Text style={{ color: C.text, fontSize: 13,
                           fontWeight: "600" }}>{t.name}</Text>
            <Text style={s.hintL2}>
              created {mmddyy(t.created_at)} · last used{" "}
              {t.last_used_at ? mmddyy(t.last_used_at) : "never"}
            </Text>
          </View>
          <Text style={[{ color: C.bad, fontSize: 12, padding: 4 },
                        revoke.isPending && { opacity: 0.5 }]}
                onPress={() => {
                  if (revoke.isPending) return;
                  Alert.alert("Revoke token",
                    `Revoke "${t.name}"? The script using it stops `
                    + "pushing immediately.",
                    [{ text: "Revoke", style: "destructive",
                       onPress: () => revoke.mutate(t.id) },
                     { text: "Cancel", style: "cancel" }]);
                }}>
            {revoke.isPending && revoke.variables === t.id
              ? "revoking…" : "revoke"}
          </Text>
        </View>
      ))}
      <TextInput style={s.input}
                 placeholder={read ? "what will use it (e.g. grafana)"
                                   : "script name (e.g. my-bank)"}
                 placeholderTextColor={C.mut}
                 autoCapitalize="none" value={name}
                 onChangeText={setName} />
      {msg ? (
        <Text style={{ color: C.bad, fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg}</Text>
      ) : null}
      <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 6 },
                   (!name.trim() || mint.isPending) && { opacity: 0.5 }]}
                 disabled={!name.trim() || mint.isPending}
                 onPress={() => mint.mutate()}>
        <Text style={s.btnText}>
          {mint.isPending ? "creating…" : "Create token"}
        </Text>
      </Pressable>
    </Card>
  );
}


// ---- Integrations: webhooks out, the read doors in (the web's cards) ----
const ALL_EVENTS = "*";

function EventChips({ events, value, onChange }: {
  events: WebhookEvent[]; value: string[]; onChange: (v: string[]) => void;
}) {
  const all = value.includes(ALL_EVENTS);
  const toggle = (name: string) => {
    if (name === ALL_EVENTS) return onChange(all ? [] : [ALL_EVENTS]);
    if (all) return;
    onChange(value.includes(name) ? value.filter((v) => v !== name)
                                  : [...value, name]);
  };
  return (
    <View style={{ flexDirection: "row", flexWrap: "wrap", gap: 6,
                   marginTop: 6 }}>
      {[{ name: ALL_EVENTS, description: "every event" }, ...events]
        .map((ev) => {
          const on = all ? true : value.includes(ev.name);
          return (
            <Pressable key={ev.name} style={[s.chip, on && s.chipOn,
                         all && ev.name !== ALL_EVENTS && { opacity: 0.5 }]}
                       onPress={() => toggle(ev.name)}>
              <Text style={{ color: on ? C.text : C.mut, fontSize: 12 }}>
                {ev.name === ALL_EVENTS ? "every event" : ev.name}
              </Text>
            </Pressable>
          );
        })}
    </View>
  );
}

function WebhookRow({ hook }: { hook: Webhook }) {
  const { client } = useSession();
  const qc = useQueryClient();
  const [msg, setMsg] = useState<{ text: string; bad?: boolean } | null>(null);
  const patch = (row: Webhook) =>
    qc.setQueryData<{ webhooks: Webhook[]; events: WebhookEvent[] }>(
      ["webhooks"], (o) => o && { ...o,
        webhooks: o.webhooks.map((w) => (w.id === row.id ? row : w)) });
  const toggle = useMutation({
    mutationFn: (enabled: boolean) =>
      client!.webhookUpdate(hook.id, { enabled }),
    onSuccess: (row) => { patch(row); setMsg(null); },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  const test = useMutation({
    mutationFn: () => client!.webhookTest(hook.id),
    onSuccess: (r) => {
      setMsg(r.ok ? { text: `Delivered — HTTP ${r.status}.` }
                  : { text: `Not delivered: ${r.error ?? "no answer"}`,
                      bad: true });
      qc.invalidateQueries({ queryKey: ["webhooks"] });
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  const remove = useMutation({
    mutationFn: () => client!.webhookDelete(hook.id),
    onSuccess: () => {
      removeFromList(qc, ["webhooks"], "webhooks",
                     (w: { id: string }) => w.id === hook.id);
      qc.invalidateQueries({ queryKey: ["webhooks"] });
    },
    onError: (e) => setMsg({ text: errText(e), bad: true }),
  });
  const busy = toggle.isPending || test.isPending || remove.isPending;
  const status = hook.disabled_reason ? "switched off"
    : hook.last_status == null ? "never sent"
    : hook.last_error ? `last: ${hook.last_error.slice(0, 40)}`
    : `last HTTP ${hook.last_status}`;
  return (
    <View style={{ borderTopColor: C.border,
                   borderTopWidth: StyleSheet.hairlineWidth,
                   paddingVertical: 8, gap: 4 }}>
      <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <View style={{ flex: 1 }}>
          <Text style={{ color: C.text, fontSize: 13, fontWeight: "600" }}>
            {hook.name || "webhook"}
          </Text>
          <Text style={s.hintL2} numberOfLines={1}>{hook.url}</Text>
        </View>
        <Switch value={hook.enabled} disabled={busy}
                onValueChange={(v) => toggle.mutate(v)}
                trackColor={{ true: C.accent, false: C.hover }}
                thumbColor={C.text} />
      </View>
      <Text style={s.hintL2}>
        {hook.events.includes(ALL_EVENTS) ? "every event"
          : hook.events.join(" · ")}
        {" · "}{status}
        {hook.last_attempt_at ? ` · ${mmddyy(hook.last_attempt_at)}` : ""}
      </Text>
      {hook.disabled_reason ? (
        <Text style={{ color: C.bad, fontSize: 12 }}>
          {hook.disabled_reason} — fix the receiver, then switch it back on.
        </Text>
      ) : null}
      {msg ? (
        <Text style={{ color: msg.bad ? C.bad : C.good, fontSize: 12 }}
              onPress={() => setMsg(null)}>{msg.text}</Text>
      ) : null}
      <View style={{ flexDirection: "row", gap: 8, marginTop: 2 }}>
        <Pressable style={[s.btn, s.btnQuiet, busy && { opacity: 0.5 }]}
                   disabled={busy} onPress={() => test.mutate()}>
          <Text style={s.btnText}>
            {test.isPending ? "sending…" : "Send a test"}</Text>
        </Pressable>
        <View style={{ flex: 1 }} />
        <Text style={[{ color: C.bad, fontSize: 12, padding: 6 },
                      busy && { opacity: 0.5 }]}
              onPress={() => {
                if (busy) return;
                Alert.alert("Delete webhook",
                  `Delete "${hook.name || hook.url}"? Nothing further `
                  + "will be sent to it.",
                  [{ text: "Delete", style: "destructive",
                     onPress: () => remove.mutate() },
                   { text: "Cancel", style: "cancel" }]);
              }}>
          {remove.isPending ? "deleting…" : "delete"}
        </Text>
      </View>
    </View>
  );
}

function WebhooksCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["webhooks"],
    queryFn: () => client!.webhooks(), enabled: !!client });
  const [adding, setAdding] = useState(false);
  const [url, setUrl] = useState("");
  const [name, setName] = useState("");
  const [evs, setEvs] = useState<string[]>([]);
  const [fresh, setFresh] =
    useState<{ name: string; secret: string } | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  // the ledger gets posted to this URL — elevation-gated, like a token
  const create = useMutation({
    mutationFn: () => client!.webhookCreate({ url: url.trim(), events: evs,
                                              name: name.trim() }),
    onSuccess: (r) => {
      const { secret, ...row } = r;
      setFresh({ name: row.name || row.url, secret });
      setUrl(""); setName(""); setEvs([]); setAdding(false); setMsg(null);
      qc.setQueryData<typeof q.data>(["webhooks"], (o) => o && { ...o,
        webhooks: [...o.webhooks, row as Webhook] });
      qc.invalidateQueries({ queryKey: ["webhooks"] });
    },
    onError: (e) => setMsg(stepUpMsg(e)),
  });
  const hooks = q.data?.webhooks ?? [];
  return (
    <Card>
      <H>Webhooks</H>
      <Text style={s.hintL}>
        Tell another program when something happens: new charges from a
        sync, an alert, the daily verdict, a bill someone edited. Each
        event is one signed HTTP POST to a URL you choose. Failed
        deliveries retry for a day; a receiver that stays down gets
        switched off and says so here.
      </Text>
      {fresh ? (
        <View style={{ backgroundColor: C.bg, borderColor: C.good,
                       borderWidth: 1, borderRadius: 8, padding: 8,
                       marginTop: 6, gap: 4 }}>
          <Text style={s.hintL}>
            {fresh.name} — copy the signing secret now; it won&apos;t be
            shown again:
          </Text>
          <Text style={{ color: C.text, fontFamily: "monospace",
                         fontSize: 12 }} selectable>
            {fresh.secret}
          </Text>
          <Pressable style={[s.btn, { alignSelf: "flex-start" }]}
                     onPress={async () => {
                       await Clipboard.setStringAsync(fresh.secret);
                       setFresh(null);
                     }}>
            <Text style={s.btnText}>Copy &amp; dismiss</Text>
          </Pressable>
        </View>
      ) : null}
      {hooks.map((h) => <WebhookRow key={h.id} hook={h} />)}
      {!hooks.length && !q.isPending ? (
        <Text style={[s.hintL2, { marginTop: 6 }]}>No webhooks yet.</Text>
      ) : null}
      {adding ? (
        <View style={{ marginTop: 6 }}>
          <TextInput style={s.input} placeholder="name (e.g. home-assistant)"
                     placeholderTextColor={C.mut} autoCapitalize="none"
                     value={name} onChangeText={setName} />
          <TextInput style={s.input} placeholder="https://receiver.example/hook"
                     placeholderTextColor={C.mut} autoCapitalize="none"
                     autoCorrect={false} keyboardType="url"
                     value={url} onChangeText={setUrl} />
          <EventChips events={q.data?.events ?? []} value={evs}
                      onChange={setEvs} />
          {msg ? (
            <Text style={{ color: C.bad, fontSize: 12, marginTop: 4 }}
                  onPress={() => setMsg(null)}>{msg}</Text>
          ) : null}
          <View style={{ flexDirection: "row", gap: 8, marginTop: 8 }}>
            <Pressable style={[s.btn, (create.isPending || !url.trim()
                                        || !evs.length) && { opacity: 0.5 }]}
                       disabled={create.isPending || !url.trim() || !evs.length}
                       onPress={() => create.mutate()}>
              <Text style={s.btnText}>
                {create.isPending ? "creating…" : "Create webhook"}</Text>
            </Pressable>
            <Pressable style={[s.btn, s.btnQuiet]}
                       onPress={() => setAdding(false)}>
              <Text style={s.btnText}>cancel</Text>
            </Pressable>
          </View>
        </View>
      ) : (
        <Pressable style={[s.btn, { alignSelf: "flex-start", marginTop: 8 }]}
                   onPress={() => setAdding(true)}>
          <Text style={s.btnText}>Add a webhook</Text>
        </Pressable>
      )}
    </Card>
  );
}

// where the read doors are, with this instance's address filled in; the
// paste-ready snippets live on the web card and in the guide
function EndpointsCard() {
  const { serverUrl } = useSession();
  const base = (serverUrl ?? "").replace(/\/$/, "");
  return (
    <Card>
      <H>Read endpoints</H>
      <Text style={s.hintL}>
        Every one takes an integration token as a bearer. The summary is
        one JSON document; /metrics is the same numbers in the Prometheus
        text format; the query doors behind the MCP server search the
        ledger, spending, bills and the net-worth series.
      </Text>
      <KV k="summary" v={`${base}/api/integrations/summary`} tone="mut" />
      <KV k="metrics" v={`${base}/metrics`} tone="mut" />
      <KV k="search" v={`${base}/api/integrations/transactions?q=…`}
          tone="mut" />
      <HelpLink topic="integrations" />
    </Card>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  signOut: { backgroundColor: C.card, borderColor: C.bad, borderWidth: 1,
             borderRadius: C.r, marginHorizontal: 12, marginTop: 16,
             paddingVertical: 12, alignItems: "center" },
  signOutText: { color: C.bad, fontSize: 15, fontWeight: "600" },
  hint: { color: C.mut, fontSize: 12, textAlign: "center", marginTop: 10,
          marginHorizontal: 20 },
  testBtn: { backgroundColor: C.hover, borderRadius: 8, marginTop: 10,
             paddingVertical: 10, alignItems: "center" },
  hintL: { color: C.mut, fontSize: 12, lineHeight: 17 },
  hintL2: { color: C.mut, fontSize: 11 },
  btn: { backgroundColor: C.accent, borderRadius: 8,
         paddingHorizontal: 14, paddingVertical: 8 },
  btnQuiet: { backgroundColor: C.hover },
  btnText: { color: C.text, fontSize: 13, fontWeight: "600" },
  chip: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
          borderRadius: 999, paddingHorizontal: 10, paddingVertical: 5 },
  chipOn: { borderColor: C.accent, backgroundColor: C.hover },
  input: { backgroundColor: C.bg, borderColor: C.border, borderWidth: 1,
           borderRadius: 8, color: C.text, fontSize: 14,
           paddingHorizontal: 10, paddingVertical: 8, marginTop: 6 },
  search: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
            borderRadius: 8, color: C.text, fontSize: 14,
            paddingHorizontal: 12, paddingVertical: 9,
            marginHorizontal: 12, marginTop: 14 },
});


// ---- default home page, per client (mirrors the web's Settings card) ----
// Household-level like `theme`; both halves editable from either client.
const WEB_HOMES: [string, string][] = [
  ["/", "Today"], ["/transactions", "Transactions"], ["/bills", "Bills"],
  ["/accounts", "Accounts"], ["/networth", "Net worth"],
  ["/budget", "Budget"], ["/cashflow", "Cash flow"],
  ["/reimburse", "Reimburse"], ["/business", "Business"],
];
const MOBILE_HOMES: [string, string][] = [
  ["index", "Today"], ["transactions", "Transactions"], ["bills", "Bills"],
  ["accounts", "Accounts"], ["more", "More"],
];
function HomePageCard() {
  const { client } = useSession();
  const qc = useQueryClient();
  const viewer = useViewer();
  const q = useQuery({ queryKey: ["settings"],
    queryFn: () => client!.settingsFull(), enabled: !!client });
  const me = useQuery({ queryKey: ["me"],
    queryFn: () => client!.me(), enabled: !!client });
  // Per-KEY request sequencing, as on the web card: a slower request for
  // the same key must neither roll back nor overwrite a later one that
  // already landed — saveSettings seeds the WHOLE returned document, so
  // even a slow SUCCESS would revert a newer choice unless the newest
  // value is re-applied on top of it.
  const seq = useRef<Record<string, number>>({});
  const latest = useRef<Record<string, string>>({});
  const set = (key: "home_web" | "home_mobile", value: string) => {
    const prev = q.data?.[key];
    const mine = (seq.current[key] = (seq.current[key] ?? 0) + 1);
    latest.current[key] = value;
    patchQuery<SettingsView>(qc, ["settings"], { [key]: value });
    saveSettings(qc, client!, { [key]: value })
      .then(() => {
        if (mine !== seq.current[key])       // a newer choice landed after
          patchQuery<SettingsView>(qc, ["settings"],
                                   { [key]: latest.current[key] });
      })
      .catch((e) => { if (mine !== seq.current[key]) return;
                      Alert.alert("Couldn't save", errText(e));
                      patchQuery<SettingsView>(qc, ["settings"],
                                               { [key]: prev }); });
  };
  // a viewer cannot write settings — editing chrome stays hidden, as on
  // every other card here
  if (viewer || !q.data) return null;
  // no entity, no Business as a home page
  const webHomes = WEB_HOMES.filter(([r]) =>
    r !== "/business" || me.data?.has_business);
  const row = (label: string, opts: [string, string][],
               cur: string, key: "home_web" | "home_mobile") => (
    <>
      <Text style={{ color: C.mut, fontSize: 12, marginTop: 8 }}>{label}</Text>
      <View style={{ flexDirection: "row", gap: 8, marginTop: 4,
                     flexWrap: "wrap" }}>
        {opts.map(([v, l]) => (
          <Pressable key={v} style={[s.chip, cur === v && s.chipOn]}
                     onPress={() => set(key, v)}>
            <Text style={{ color: cur === v ? C.text : C.mut,
                           fontSize: 13 }}>{l}</Text>
          </Pressable>
        ))}
      </View>
    </>
  );
  return (
    <Card>
      <H>Home page</H>
      {row("This app opens on", MOBILE_HOMES,
           q.data.home_mobile || "index", "home_mobile")}
      {row("Web app opens on", webHomes, q.data.home_web || "/",
           "home_web")}
      <Text style={s.hintL}>Applied once per launch — the Today tab is
        always a tap away.</Text>
    </Card>
  );
}
