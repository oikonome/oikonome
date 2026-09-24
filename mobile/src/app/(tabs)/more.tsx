// More — the rest of the product, native. The only web-only surfaces
// left is passkey sign-in (the footer says so); everything else ships
// here too.
import { Ionicons } from "@expo/vector-icons";
import { useQuery } from "@tanstack/react-query";
import { useRouter, useScrollToTop } from "expo-router";
import { useRef } from "react";
import { Linking, Pressable, ScrollView, StyleSheet, Text, View } from "react-native";

import { useSession } from "../../lib/session";
import { ext } from "../../ext";
import { C } from "../../lib/theme";
import { useViewer } from "../../lib/viewer";

type IconName = keyof typeof Ionicons.glyphMap;
const PAGES: [string, string, string, IconName][] = [
  // the guided first-run walkthrough lives in Settings → Wizards, with the
  // other wizards; Today's checklist links to it while setup is unfinished
  ["Budget", "/budget", "the plan: income, categories, savings",
   "pie-chart-outline"],
  ["This month", "/month", "verdict, buckets, bills, biggest",
   "calendar-outline"],
  ["This year", "/year", "twelve months at a glance", "grid-outline"],
  ["Spending", "/spending", "years, months, merchants, categories",
   "bar-chart-outline"],
  ["Net Worth", "/networth", "total, institutions, asset classes",
   "trending-up-outline"],
  ["Cash Flow", "/cashflow", "savings rate, income, net cash",
   "stats-chart-outline"],
  ["Debt", "/debt", "payoff order, extra payment, debt-free date",
   "trending-down-outline"],
  ["Retire", "/retire", "the projection and stress tests",
   "hourglass-outline"],
  ["Reimburse", "/reimburse", "flag + pair paybacks",
   "swap-horizontal-outline"],
  ["Rules", "/rules", "auto-categorization", "funnel-outline"],
  ["Business", "/business", "entities and flagged expenses",
   "briefcase-outline"],
  ["Import & sync", "/imports", "pull banks now, recent imports",
   "cloud-download-outline"],
  ["Assistant", "/assistant", "ask about your money",
   "chatbubble-ellipses-outline"],
  ["Items", "/items", "receipt line items, searchable",
   "pricetags-outline"],
  ["Activity", "/activity", "who changed what, and when",
   "footsteps-outline"],
  ["Alerts", "/alerts", "what the app has flagged",
   "notifications-outline"],
  ["Doctor", "/doctor", "instance health checks", "medkit-outline"],
  ["Email & Push", "/notifications",
   "daily/weekly summaries: email, push + SMS", "mail-outline"],
  ["Settings", "/settings", "account, devices, sign out",
   "settings-outline"],
  ["Security & household", "/security",
   "password, 2FA, sessions, members, invites",
   "shield-checkmark-outline"],
  ["Help", "/help", "docs and FAQ", "help-circle-outline"],
  ["Feedback & bug reports", "/feedback",
   "tell the operator what broke or what you'd change",
   "megaphone-outline"],
];

// The version opens the release notes index on GitHub. Same as the web.
const RELEASES = "https://github.com/oikonome/oikonome/releases";

export default function More() {
  const router = useRouter();
  // re-tapping the active tab scrolls back to the top
  const topRef = useRef<ScrollView>(null);
  useScrollToTop(topRef);
  const { client } = useSession();
  const viewer = useViewer();
  // the web hides destinations that don't apply: Assistant until the
  // backend answers, Business until an entity exists, Doctor on hosted,
  // Feedback when the operator turned it off / on demo
  // the tab bar's badge is this count; the row it points at should say
  // so — a bare "1" on More reads as a mystery
  const alerts = useQuery({ queryKey: ["alerts"],
                            queryFn: () => client!.alertsHistory(),
                            enabled: !!client, staleTime: 60_000 });
  const unread = (alerts.data?.alerts ?? [])
    .filter((a) => a.active && !a.dismissed).length;
  const me = useQuery({ queryKey: ["me"], queryFn: () => client!.me(),
    enabled: !!client, staleTime: 5 * 60_000 });
  // feedback can be switched OFF by the operator (the web hides its icon
  // then) — the settings view is the one place that says so
  const fb = useQuery({ queryKey: ["settings-feedback"],
    queryFn: () => client!.req<{ feedback_enabled: boolean | null }>(
      "/api/settings"),
    enabled: !!client, staleTime: 5 * 60_000 });
  const assistant = useQuery({ queryKey: ["assistant-status"],
    queryFn: () => client!.assistantStatus(), enabled: !!client,
    staleTime: 5 * 60_000 });
  const hidden = new Set<string>();
  if (assistant.data && !assistant.data.available)
    hidden.add("/assistant");
  if (!me.data?.has_business) hidden.add("/business");
  if (me.data?.hosted) hidden.add("/doctor");
  if (me.data?.demo) hidden.add("/feedback");
  if (fb.data?.feedback_enabled === false) hidden.add("/feedback");
  // the cadence matrix is the OWNER's household schedule — the web gives a
  // viewer no route to it at all, only their own delivery switch in
  // Settings, and every write here 403s
  if (viewer) hidden.add("/notifications");
  return (
    <ScrollView ref={topRef} style={s.wrap}
      contentContainerStyle={{ paddingBottom: 24 }}>
      <View style={s.group}>
        {PAGES.filter(([, href]) => !hidden.has(href))
          .map(([label, href, sub, icon]) => (
          <Pressable key={href} onPress={() => router.push(href as never)}
                     style={({ pressed }) => [s.item,
                       pressed && { backgroundColor: C.hover }]}>
            <Ionicons name={icon} size={20} color={C.accent}
                      style={{ width: 26 }} />
            <View style={{ flex: 1 }}>
              <Text style={s.label}>{label}</Text>
              <Text style={s.sub}>
                {href === "/alerts" && unread > 0
                  ? `${unread} active — the badge on this tab`
                  : sub}
              </Text>
            </View>
            {href === "/alerts" && unread > 0 && (
              <View style={s.pill}><Text style={s.pillText}>{unread}</Text></View>
            )}
            <Text style={s.chev}>›</Text>
          </Pressable>
        ))}
      </View>
      <Text style={s.foot}>
        Everything the web app does lives here too.
      </Text>
      {/* the web footer's identity line — version, privacy, and the
          support link when the operator publishes one */}
      {me.data && (
        <Text style={[s.foot, { marginTop: 2 }]}>
          <Text style={{ color: C.accent }}
                onPress={() => Linking.openURL(RELEASES)}>
            Oikonome {me.data.version}
          </Text>
          {ext.legal.privacy ? (
            <>
              {" · "}
              <Text style={{ color: C.accent }}
                    onPress={() => Linking.openURL(ext.legal.privacy!)}>
                Privacy
              </Text>
            </>
          ) : null}
          {me.data.donate_url ? (
            <>
              {" · "}
              <Text style={{ color: C.accent }}
                    onPress={() =>
                      Linking.openURL(me.data!.donate_url!)}>
                ♥ Support development
              </Text>
            </>
          ) : null}
        </Text>
      )}
    </ScrollView>
  );
}

const s = StyleSheet.create({
  wrap: { flex: 1, backgroundColor: C.bg },
  group: { backgroundColor: C.card, borderColor: C.border, borderWidth: 1,
           borderRadius: C.r, marginHorizontal: 12, marginTop: 12,
           overflow: "hidden" },
  item: { flexDirection: "row", alignItems: "center", gap: 8,
          paddingHorizontal: 14, paddingVertical: 11,
          borderTopColor: C.border,
          borderTopWidth: StyleSheet.hairlineWidth },
  label: { color: C.text, fontSize: 15, fontWeight: "600" },
  sub: { color: C.mut, fontSize: 12 },
  pill: { minWidth: 22, paddingHorizontal: 6, paddingVertical: 2,
          borderRadius: 11, backgroundColor: C.accent, marginRight: 8,
          alignItems: "center" },
  pillText: { color: C.bg, fontSize: 12, fontWeight: "700" },
  chev: { color: C.mut, fontSize: 20 },
  foot: { color: C.mut, fontSize: 12, textAlign: "center", marginTop: 12,
          marginHorizontal: 24 },
});
