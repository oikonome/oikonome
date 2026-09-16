import { focusManager } from "@tanstack/react-query";
import { PersistQueryClientProvider } from "@tanstack/react-query-persist-client";
import { Stack, usePathname, useRouter } from "expo-router";
import { StatusBar } from "expo-status-bar";
import { useEffect, useState } from "react";
import { AppState, Platform, View } from "react-native";
import * as ScreenCapture from "expo-screen-capture";

import ElevationModal from "../components/elevation-modal";
import { ext } from "../ext";
import { PINNED_SERVER_URL } from "../lib/api";
import { markLaunchConsumed, settleLaunch } from "../lib/launch";
import { pushSupported } from "../lib/push";
import { PERSIST_BUSTER, persister, queryClient } from "../lib/query";
import { SessionProvider, useSession } from "../lib/session";
import { C, isDarkTheme } from "../lib/theme";

/** Warm the tabs someone taps next while they read Today — a first tap
 *  on Bills/Accounts then paints from cache instead of a spinner. */
function Prefetcher() {
  const { phase, client } = useSession();
  useEffect(() => {
    if (phase !== "ready" || !client) return;
    queryClient.prefetchQuery({ queryKey: ["bills"],
      queryFn: () => client.bills(), staleTime: 60_000 });
    queryClient.prefetchQuery({ queryKey: ["accounts"],
      queryFn: () => client.accounts(), staleTime: 60_000 });
    queryClient.prefetchQuery({ queryKey: ["alerts"],
      queryFn: () => client.alertsHistory(), staleTime: 60_000 });
  }, [phase, client]);
  return null;
}

// Steers, never unmounts: the navigator must stay mounted for the whole
// app lifetime. Swapping the <Stack> for a <Redirect> navigates with the
// navigator unmounted, which makes expo-router remount the root layout —
// resetting SessionProvider to "locked" and trapping the app in a
// biometric-prompt loop.
function Gate({ children }: { children: React.ReactNode }) {
  const { phase, captureAllowed } = useSession();
  const path = usePathname();
  const router = useRouter();

  // Every screen behind the gate shows money, and the security screen
  // shows recovery codes: while the session is ready the window refuses
  // screenshots and screen recording (Android FLAG_SECURE, which also
  // blanks the recents thumbnail; iOS blocks capture) and iOS blurs the
  // app-switcher snapshot. Released outside "ready" so the connect /
  // login screens stay shareable for support.
  // The person can turn the block off (Settings → Security) — to record a
  // walkthrough, share a screen with support, or because it is their
  // phone. Default stays on.
  useEffect(() => {
    if (phase !== "ready" || captureAllowed) return;
    ScreenCapture.preventScreenCaptureAsync("session").catch(() => {});
    if (Platform.OS === "ios")
      ScreenCapture.enableAppSwitcherProtectionAsync().catch(() => {});
    return () => {
      ScreenCapture.allowScreenCaptureAsync("session").catch(() => {});
      if (Platform.OS === "ios")
        ScreenCapture.disableAppSwitcherProtectionAsync().catch(() => {});
    };
  }, [phase, captureAllowed]);

  // and the app's own cover goes up the moment it leaves the foreground,
  // so what the switcher / recents shows is the brand colour, not the
  // ledger — belt to the native braces above
  const [inFront, setInFront] = useState(AppState.currentState === "active");
  useEffect(() => {
    const sub = AppState.addEventListener("change", (st) =>
      setInFront(st === "active"));
    return () => sub.remove();
  }, []);

  useEffect(() => {
    if (phase === "loading") return;
    const inSetup = path === "/connect" || path === "/login";
    // a build pinned to one server skips the URL screen entirely
    if (phase === "setup" && !inSetup)
      router.replace(PINNED_SERVER_URL ? "/login" : "/connect");
    else if (phase === "locked" && path !== "/unlock") router.replace("/unlock");
    else if (phase === "ready" && (inSetup || path === "/unlock"))
      router.replace("/");
  }, [phase, path, router]);

  // the cover hides screens until the gate has steered — the navigator
  // stays mounted underneath it
  const covered = phase === "loading" ||
    (phase === "locked" && path !== "/unlock") ||
    (phase === "ready" && !inFront);
  return (
    <View style={{ flex: 1 }}>
      {children}
      {covered ? (
        <View style={{ position: "absolute", top: 0, left: 0, right: 0,
                       bottom: 0, backgroundColor: C.bg }} />
      ) : null}
    </View>
  );
}

export default function RootLayout() {
  // React Query only refetches stale data "on focus" if something tells
  // it focus happened — on native that signal is AppState. Without this,
  // reopening the app shows day-old numbers until a manual pull.
  useEffect(() => {
    const sub = AppState.addEventListener("change", (st) => {
      focusManager.setFocused(st === "active");
    });
    return () => sub.remove();
  }, []);

  // a push is a tickle — its body is at most the daily verdict line, so
  // receiving one just means "your server has something newer than your
  // cache"; tapping one lands on the page its event kind is about
  const router = useRouter();
  useEffect(() => {
    if (!pushSupported()) { settleLaunch(); return; }
    const subs: { remove(): void }[] = [];
    // the tap that opened the app is handled once — a cold start hands
    // it to getLastNotificationResponseAsync, a warm one to the response
    // listener, and some platforms to both
    const handled = new Set<string>();
    // refresh what the event is ABOUT, not every mounted observer: a
    // bare invalidate would refetch Today, Bills, Accounts, the alert
    // history and the whole stack behind every tickle
    const refreshFor = (event: string) => {
      const inv = (key: string[]) =>
        queryClient.invalidateQueries({ queryKey: key });
      if (event === "monthly") inv(["lens", "month"]);
      else if (event === "yearly") inv(["lens", "year"]);
      else { inv(["today"]); inv(["alerts"]); }
    };
    import("expo-notifications").then((N) => {
      // a push that lands while the app is FOREGROUND is handed to the
      // app instead of shown — without this handler it vanishes, including
      // "send test push", which is always tapped inside the app
      N.setNotificationHandler({
        handleNotification: async () => ({
          shouldShowBanner: true, shouldShowList: true,
          shouldPlaySound: false, shouldSetBadge: false,
        }),
      });
      subs.push(N.addNotificationReceivedListener((n) => {
        refreshFor(String(n.request.content.data?.event ?? ""));
      }));
      const onTap = (resp: { notification: { request: {
        identifier: string; content: { data?: Record<string, unknown> } } } }) => {
        const id = resp.notification.request.identifier;
        if (handled.has(id)) return;
        handled.add(id);
        const event = String(
          resp.notification.request.content.data?.event ?? "");
        markLaunchConsumed();   // the tap decides where the app opens
        refreshFor(event);
        if (event === "alert") router.push("/alerts");
        else if (event === "monthly") router.push("/month");
        else if (event === "yearly") router.push("/year");
        else router.navigate("/");
      };
      subs.push(N.addNotificationResponseReceivedListener(onTap));
      // A tap that COLD-STARTED the process was delivered before any
      // listener existed, so without asking for it the app opens on the
      // default home instead of the alert the person tapped. Ask once,
      // then let the home redirect proceed either way.
      N.getLastNotificationResponseAsync()
        .then((resp) => { if (resp) onTap(resp); })
        .catch(() => {})
        .finally(settleLaunch);
    }).catch(settleLaunch);
    return () => subs.forEach((x) => x.remove());
  }, [router]);

  return (
    <PersistQueryClientProvider client={queryClient}
                                persistOptions={{ persister,
                                  buster: PERSIST_BUSTER }}>
      <SessionProvider>
        <Prefetcher />
        <Gate>
          <Stack screenOptions={{
            headerStyle: { backgroundColor: C.bg },
            headerTintColor: C.text,
            headerShadowVisible: false,
            contentStyle: { backgroundColor: C.bg },
            // instant swaps: any transition reads as
            // latency; tab switches are already instant, pushes now match
            animation: "none",
          }}>
            <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
            <Stack.Screen name="connect" options={{ title: "Connect" }} />
            <Stack.Screen name="login" options={{ title: "Sign in" }} />
            <Stack.Screen name="unlock" options={{ headerShown: false }} />
            <Stack.Screen name="txn" options={{ title: "Transaction" }} />
            <Stack.Screen name="bill" options={{ title: "Bill" }} />
            <Stack.Screen name="bill-history"
                          options={{ title: "History" }} />
            <Stack.Screen name="budget" options={{ title: "Budget" }} />
            <Stack.Screen name="month" options={{ title: "Month" }} />
            <Stack.Screen name="year" options={{ title: "Year" }} />
            <Stack.Screen name="spending" options={{ title: "Spending" }} />
            <Stack.Screen name="networth" options={{ title: "Net worth" }} />
            <Stack.Screen name="cashflow" options={{ title: "Cash flow" }} />
            <Stack.Screen name="alerts" options={{ title: "Alerts" }} />
            <Stack.Screen name="items" options={{ title: "Items" }} />
            <Stack.Screen name="doctor" options={{ title: "Doctor" }} />
            <Stack.Screen name="retire" options={{ title: "Retire" }} />
            <Stack.Screen name="reimburse" options={{ title: "Reimburse" }} />
            <Stack.Screen name="rules" options={{ title: "Rules" }} />
            <Stack.Screen name="business" options={{ title: "Business" }} />
            <Stack.Screen name="merchant" options={{ title: "Merchant" }} />
            <Stack.Screen name="imports"
                          options={{ title: "Import & sync" }} />
            <Stack.Screen name="assistant" options={{ title: "Assistant" }} />
            <Stack.Screen name="settings" options={{ title: "Settings" }} />
            {/* an installed add-on's screens — every screen needs its own
                entry, since an unregistered route falls back to its FILENAME
                as the title */}
            {ext.screens.map((sc) => (
              <Stack.Screen key={sc.name} name={sc.name}
                            options={{ title: sc.title }} />
            ))}
            <Stack.Screen name="welcome" options={{ title: "Setup" }} />
            <Stack.Screen name="security"
                          options={{ title: "Security & household" }} />
            <Stack.Screen name="notifications"
                          options={{ title: "Email & Push" }} />
            <Stack.Screen name="help" options={{ title: "Help" }} />
            <Stack.Screen name="feedback"
                          options={{ title: "Feedback & bug reports" }} />
          </Stack>
        </Gate>
        {/* the one "confirm it's you" sheet every guarded call shares —
            outside the Gate so it never depends on which screen asked */}
        <ElevationModal />
        <StatusBar style={isDarkTheme ? "light" : "dark"} />
      </SessionProvider>
    </PersistQueryClientProvider>
  );
}
