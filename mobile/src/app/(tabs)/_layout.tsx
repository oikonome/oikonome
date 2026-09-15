import { Ionicons } from "@expo/vector-icons";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Tabs, useRouter } from "expo-router";
import { useEffect, useRef, useState } from "react";
import { Alert, ColorValue, Image, Pressable, Text,
         View } from "react-native";

import LensSwitch from "../../components/lens-switch";
import { Lockout } from "../../components/lockout";
import { ext } from "../../ext";
import { seedSettings } from "../../lib/cache";
import { useSession } from "../../lib/session";
import { deviceZone } from "../../lib/zone";
import { C, getThemeMode, setThemeMode,
         type ThemeMode } from "../../lib/theme";
import { errText } from "../../lib/api";

// the brand row the web header carries — mark + wordmark
function Brand() {
  return (
    <View style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
      <Image source={require("../../../assets/images/splash-icon.png")}
             style={{ width: 22, height: 22 }} />
      <Text style={{ color: C.text, fontSize: 19, fontWeight: "700" }}>
        Oikonome
      </Text>
    </View>
  );
}

// The web header's alerts bell, on every tab: a tap opens /alerts, the
// badge is the active-alert count in the worst active severity's colour.
// The More tab keeps its badge too — the bell is where the web puts it,
// the tab badge is what the phone already had.
function AlertsBell({ count, tone }: { count: number; tone: string }) {
  const router = useRouter();
  return (
    <Pressable onPress={() => router.push("/alerts" as never)}
               hitSlop={8}
               accessibilityLabel={count > 0
                 ? `Alerts, ${count} active` : "Alerts"}
               style={{ marginRight: 14, padding: 2 }}>
      <Ionicons name="notifications-outline" size={22} color={C.text} />
      {count > 0 && (
        <View style={{ position: "absolute", top: -3, right: -6,
                       minWidth: 16, height: 16, borderRadius: 8,
                       paddingHorizontal: 3, alignItems: "center",
                       justifyContent: "center",
                       backgroundColor: tone === "bad" ? C.bad
                         : tone === "warn" ? C.warn : C.accent }}>
          <Text style={{ color: C.bg, fontSize: 10, fontWeight: "700" }}>
            {count > 99 ? "99+" : count}
          </Text>
        </View>
      )}
    </Pressable>
  );
}

type IconName = keyof typeof Ionicons.glyphMap;
// the tab glyphs stay brand-blue in both states, like the More list and
// the web rail — focus reads from the fill (solid vs outline) and label
const icon = (name: IconName) =>
  ({ focused }: { color: ColorValue; focused: boolean }) =>
    <Ionicons name={focused ? name : `${name}-outline` as IconName}
              size={22} color={C.accent} />;

// reboot choreography for a signed-in phone (the web's RebootCountdown):
// count down to the broadcast deadline, then a full-screen restarting
// state that polls /readyz and refreshes every query the moment the
// server answers again — the user always sees SOMETHING, and gets back
// in without lifting a finger.
function RebootCountdown({ deadline, message, serverUrl, onBack }: {
  deadline: string; message: string; serverUrl: string | null;
  onBack: () => void;
}) {
  const [now, setNow] = useState(Date.now());
  const [serverBack, setServerBack] = useState(false);
  const left = Math.max(0,
    Math.round((new Date(deadline).getTime() - now) / 1000));
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const down = left === 0;
  useEffect(() => {
    if (!down || serverBack || !serverUrl) return;
    // give the box a moment to actually go down, then poll until it
    // answers — only trust an OK that arrives after the drop window
    let stop = false;
    const started = Date.now();
    const poll = () => {
      if (stop) return;
      fetch(`${serverUrl}/readyz`, { cache: "no-store" })
        .then((r) => {
          if (r.ok && Date.now() - started > 20_000) {
            setServerBack(true);
            setTimeout(onBack, 800);
          } else setTimeout(poll, 4000);
        })
        .catch(() => setTimeout(poll, 4000));
    };
    const t = setTimeout(poll, 8000);
    return () => { stop = true; clearTimeout(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [down, serverBack, serverUrl]);
  if (left > 0)
    return (
      <Text style={{ color: C.bad, fontSize: 12, textAlign: "center",
                     paddingHorizontal: 12, paddingTop: 6,
                     fontVariant: ["tabular-nums"] }}>
        ⚠ {message} — restarting in {left}s
      </Text>
    );
  if (serverBack)
    return (
      <Text style={{ color: C.good, fontSize: 12, textAlign: "center",
                     paddingTop: 6 }}>
        Back — reconnected.
      </Text>
    );
  return (
    <View style={{ position: "absolute", left: 0, right: 0, top: 0,
                   bottom: 0, zIndex: 999, backgroundColor: C.bg,
                   alignItems: "center", justifyContent: "center",
                   padding: 32, gap: 12 }}>
      <Text style={{ color: C.text, fontSize: 18, fontWeight: "700" }}>
        Restarting the server
      </Text>
      <Text style={{ color: C.mut, fontSize: 13, textAlign: "center",
                     lineHeight: 19 }}>
        Routine maintenance, about a minute. The app reconnects by
        itself — no need to close it.
      </Text>
    </View>
  );
}

export default function TabsLayout() {
  const { client, serverUrl, signOut } = useSession();
  const router = useRouter();
  const qc = useQueryClient();
  // dismissal is keyed to the broadcast's message (the web keys its
  // sessionStorage entry the same way) — a NEW broadcast must reappear
  // after an old one was waved away
  const [bcDismissed, setBcDismissed] = useState<string | null>(null);
  const me = useQuery({
    queryKey: ["me"],
    queryFn: () => client!.me(),
    enabled: !!client,
    staleTime: 5 * 60_000,
  });
  const meErr = me.isError ? String(me.error) : "";
  // A household with no zone of its own keeps its hours on the instance's
  // clock — 2am mail for a hosted household west of the operator. The
  // first load on an OWNER's phone while the household is still on the
  // instance default and the phone is somewhere else adopts the phone's
  // zone, once; More → Notifications names it and can change it. Never
  // for a member, never on a demo, never over a zone already chosen.
  // (web: App.tsx, same rule)
  const zoneOffered = useRef(false);
  useEffect(() => {
    const m = me.data;
    if (!m || !client || zoneOffered.current || m.role !== "owner" || m.demo)
      return;
    if (m.timezone_source !== "instance") return;
    const device = deviceZone();
    if (!device || device === m.timezone) return;
    zoneOffered.current = true;
    client.settingsSaveTimezone(device)
      .then((v) => { seedSettings(qc, v);
                     qc.invalidateQueries({ queryKey: ["me"] }); })
      .catch(() => {});
  }, [me.data, client, qc]);
  // the verify banner's resend: one mail per tap, not one per tap while
  // the first is still in flight
  const [resending, setResending] = useState(false);
  // demo resets on the hour — the web counts down mm:ss; a ticking
  // clock is the honest version of "soon". Demo instances only pay
  // for the ticker.
  const [demoCountdown, setDemoCountdown] = useState("");
  const isDemo = !!me.data?.demo;
  useEffect(() => {
    if (!isDemo) return;
    const tick = () => {
      const now = new Date();
      const secs = (59 - now.getMinutes()) * 60 + (60 - now.getSeconds());
      const m = Math.floor(secs / 60);
      const s0 = secs % 60;
      setDemoCountdown(`${m}:${String(s0).padStart(2, "0")}`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [isDemo]);
  // theme follows the ACCOUNT for owners (the web's reconcile): the
  // server-stored preference wins over this device's cache; with no
  // pin the default is dark, and only an explicit "auto" follows the
  // OS. Palette resolves at bundle load, so an adopted change applies
  // at the next launch — same contract the Appearance card states.
  const themePeek = useQuery({ queryKey: ["settings-feedback"],
    queryFn: () => client!.req<{ feedback_enabled: boolean | null;
                                 theme?: string }>("/api/settings"),
    enabled: !!client, staleTime: 5 * 60_000 });
  useEffect(() => {
    if (me.data?.role !== "owner" || me.data.demo || !themePeek.data)
      return;
    const server = (themePeek.data.theme ?? "dark") as ThemeMode;
    if ((server === "auto" || server === "light" || server === "dark")
        && server !== getThemeMode())
      setThemeMode(server);
  }, [themePeek.data, me.data]);
  // the alerts bell count the web header carries — a badge on More
  const alerts = useQuery({
    queryKey: ["alerts"],
    queryFn: () => client!.alertsHistory(),
    enabled: !!client,
    staleTime: 60_000,
  });
  const unread = (alerts.data?.alerts ?? [])
    .filter((a) => a.active && !a.dismissed).length;
  // hosted instances can require 2FA; enrolment is a web flow, so the
  // app blocks here the way the web shell does instead of letting the
  // account through
  if (me.data?.needs_2fa) {
    return (
      <Lockout title="Two-factor setup required"
        body={"This server requires two-factor authentication. Sign in "
              + "on the web app to set it up, then come back."}
        onOut={signOut} />
    );
  }
  // an installed add-on's own frozen states — each brings the screen that
  // is its way out
  for (const l of ext.lockouts)
    if (l.matches(meErr))
      return <l.Screen onOut={signOut} />;
  if (meErr.includes("suspended")) {
    return (
      <Lockout title="Your account has been suspended"
        body={"Sign-in works but the account is on hold, usually while "
              + "something is sorted out with the operator. Your data is "
              + "intact — nothing has been deleted. If you think this is "
              + "a mistake, contact support and mention the email "
              + "address you sign in with."}
        onOut={signOut} />
    );
  }
  if (meErr.includes("scheduled for deletion")) {
    return (
      <Lockout title="This account is scheduled for deletion"
        body={"The grace window is still open, so nothing has been "
              + "erased yet and everything is recoverable — but the "
              + "account is frozen until someone cancels the deletion. "
              + "Contact support to cancel it, and mention the email "
              + "address you sign in with."}
        onOut={signOut} />
    );
  }
  const bounce = me.data?.email_delivery;
  const broadcast = me.data?.broadcast;
  // badge color follows the worst ACTIVE severity, like the web bell
  const worst = (alerts.data?.alerts ?? [])
    .filter((a) => a.active && !a.dismissed)
    .reduce((w, a) => (a.severity === "bad" || a.severity === "error"
      ? "bad" : w === "bad" ? w
      : a.severity === "warn" ? "warn" : w), "info" as string);
  // the verify nudge waits 3 days and yields to the bounce banner —
  // two banners about the same address contradict each other
  const created = me.data?.created_at
    ? Date.parse(String(me.data.created_at)) : NaN;
  const verifyDue = me.data?.hosted && me.data.verified === false
    && !bounce
    && (!Number.isFinite(created)
        || Date.now() - created >= 3 * 86_400_000);
  return (
    <View style={{ flex: 1, backgroundColor: C.bg }}>
      {/* a deadline broadcast is a RESTART: the web counts down, then
          overlays and reconnects itself — never a dismissible note */}
      {broadcast?.deadline ? (
        <RebootCountdown deadline={broadcast.deadline}
                         message={broadcast.message}
                         serverUrl={serverUrl}
                         onBack={() => qc.invalidateQueries()} />
      ) : broadcast && bcDismissed !== broadcast.message ? (
        <Text style={{ color: broadcast.severity === "warn"
                         ? C.warn : C.accent,
                       fontSize: 12, textAlign: "center",
                       paddingHorizontal: 12, paddingTop: 6 }}
              onPress={() => setBcDismissed(broadcast.message)}>
          {broadcast.message}
          {"  ✕"}
        </Text>
      ) : null}
      {verifyDue && (
        <Text style={{ color: C.warn, fontSize: 12,
                       textAlign: "center", paddingHorizontal: 12,
                       paddingTop: 6 }}
              onPress={() => {
                if (resending) return;
                setResending(true);
                client!.verifyResend()
                  .then((r) => Alert.alert(r.verified
                    ? "Already verified — pull to refresh."
                    : "Verification email sent."))
                  .catch((e) =>
                    Alert.alert("Resend failed", errText(e)))
                  .finally(() => setResending(false));
              }}>
          Verify your email — we sent a link to {me.data!.email}. No
          email? Tap to resend.
        </Text>
      )}
      {bounce && (
        <Text style={{ color: C.bad, fontSize: 12, textAlign: "center",
                       paddingHorizontal: 12, paddingTop: 6 }}
              onPress={() => router.push("/security" as never)}>
          Your email isn&apos;t arriving —{" "}
          {bounce.state === "complained"
            ? "it was marked as spam, so we've stopped sending it."
            : bounce.bounce_type === "HardBounce"
              ? "the mail server says the address doesn't exist."
              : "we couldn't deliver to it."}
          {bounce.did_you_mean
            ? ` Did you mean ${bounce.did_you_mean}?` : ""}
          {" Scheduled emails are paused — tap to fix the address."}
        </Text>
      )}
      {me.data?.demo && (
        <Text style={{ color: C.warn, fontSize: 12,
                       textAlign: "center", paddingHorizontal: 12,
                       paddingTop: 6 }}>
          Demo instance — synthetic data; resets in {demoCountdown}.
        </Text>
      )}
      {/* no "view-only" label, the same as the web header: the role
          shows in what a viewer can and cannot do */}
      {renderTabs(unread, worst)}
    </View>
  );
}

function renderTabs(unread: number, worst = "warn") {
  const bell = () => <AlertsBell count={unread} tone={worst} />;
  return (
    <Tabs screenOptions={{
      headerStyle: { backgroundColor: C.bg },
      headerTintColor: C.text,
      headerShadowVisible: false,
      tabBarStyle: { backgroundColor: C.card, borderTopColor: C.border },
      tabBarActiveTintColor: C.accent,
      tabBarInactiveTintColor: C.mut,
      tabBarLabelStyle: { fontSize: 11, fontWeight: "600" },
      sceneStyle: { backgroundColor: C.bg },
      // background tabs stop rendering entirely (react-freeze) — tab
      // switches spend their frame budget on the tab being shown
      freezeOnBlur: true,
      headerRight: bell,
    }}>
      <Tabs.Screen name="index"
                   options={{ title: "Oikonome",
                              // the logo, like the web header's home link —
                              // full-color when focused, dimmed otherwise
                              tabBarIcon: ({ focused }) => (
                                <Image
                                  source={require("../../../assets/images/splash-icon.png")}
                                  style={{ width: 22, height: 22,
                                           opacity: focused ? 1 : 0.45 }} />
                              ),
                              headerTitle: () => <Brand />,
                              // the lens triad rides the header, in
                              // line with the logo — it names the view
                              // of the whole screen, not one card; the
                              // bell keeps its place at the far right
                              headerRight: () => (
                                <View style={{ flexDirection: "row",
                                               alignItems: "center" }}>
                                  <LensSwitch active="today" inHeader />
                                  {bell()}
                                </View>
                              ),
                            }} />
      <Tabs.Screen name="transactions"
                   options={{ title: "Transactions", tabBarIcon: icon("list") }} />
      <Tabs.Screen name="bills"
                   options={{ title: "Bills", tabBarIcon: icon("repeat") }} />
      <Tabs.Screen name="accounts"
                   options={{ title: "Accounts",
                              tabBarIcon: icon("wallet") }} />
      <Tabs.Screen name="more"
                   options={{ title: "More",
                              tabBarIcon: icon("ellipsis-horizontal"),
                              // active-alert count, the web bell's badge
                              tabBarBadge: unread > 0 ? unread : undefined,
                              tabBarBadgeStyle: {
                                backgroundColor: worst === "bad"
                                  ? C.bad : worst === "warn"
                                    ? C.warn : C.accent,
                                color: C.bg, fontSize: 10 } }} />
    </Tabs>
  );
}
