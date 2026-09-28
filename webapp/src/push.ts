// Browser (Web Push) registration, shared by Settings and the setup
// wizard so the two doors cannot drift: same permission handling, same
// subscription shape, same reasons when it cannot happen.
import { api } from "./api/client";
import { forgetLaunchHome } from "./launchhome";

/** Notifications need a secure context and a service worker; on http://
 *  Chrome resolves requestPermission() "denied" without ever prompting,
 *  so the origin has to be tested BEFORE asking. */
export function browserPushBlocked(): boolean {
  return typeof window !== "undefined"
    && (!window.isSecureContext || !("serviceWorker" in navigator));
}

/** This browser's own push subscription, or undefined — bounded, so a
 *  worker that never becomes ready cannot hang the caller. */
async function ownPushSubscription(): Promise<PushSubscription | undefined> {
  if (typeof navigator === "undefined" || !("serviceWorker" in navigator))
    return undefined;
  try {
    const reg = await Promise.race([
      navigator.serviceWorker.ready,
      new Promise<null>((res) => setTimeout(() => res(null), 1500)),
    ]);
    if (!reg) return undefined;
    return (await reg.pushManager.getSubscription()) ?? undefined;
  } catch { return undefined; }
}

/** This browser's own subscription endpoint, or undefined. */
export async function ownPushEndpoint(): Promise<string | undefined> {
  return (await ownPushSubscription())?.endpoint;
}

/** Sign this browser out. A push subscription belongs to the browser,
 *  not the session, so on its own it outlives the sign-out and the next
 *  person on a shared browser keeps getting this household's
 *  notifications: the server is told which endpoint to drop, and the
 *  browser unsubscribes too, so the endpoint is dead even if that call
 *  never lands. The remembered launch home is this household's setting
 *  too, so it is forgotten first — before any await, so a sign-out whose
 *  network call fails still leaves the next person's launch on Today.
 *  Every sign-out button goes through here. */
export async function signOut(): Promise<void> {
  forgetLaunchHome();
  const sub = await ownPushSubscription();
  try { await api.logout(sub?.endpoint); }
  finally {
    if (sub) { try { await sub.unsubscribe(); } catch { /* already gone */ } }
  }
}

export type PushEnable = { ok: true } | { ok: false; reason: string };

export const PUSH_HTTP_REASON =
  "This page is on http://, and browsers only allow notifications on "
  + "https:// (or localhost). Give the instance a certificate with "
  + "./oikonome.sh https <domain>, or open it via localhost.";

/** Ask, subscribe, register with the server. */
export async function enableBrowserPush(vapidPublicKey: string): Promise<PushEnable> {
  if (browserPushBlocked()) return { ok: false, reason: PUSH_HTTP_REASON };
  const perm = await Notification.requestPermission();
  if (perm !== "granted") {
    return { ok: false, reason: Notification.permission === "denied"
      ? "Notifications are blocked for this site in your browser — allow "
        + "them in the address-bar site settings, then try again."
      : "Notifications were not enabled." };
  }
  const reg = await navigator.serviceWorker.ready;
  const b64 = vapidPublicKey.replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(b64 + "=".repeat((4 - (b64.length % 4)) % 4));
  const key = Uint8Array.from(raw, (c) => c.charCodeAt(0));
  const sub = await reg.pushManager.subscribe({
    userVisibleOnly: true, applicationServerKey: key });
  await api.pushSubscribe(sub.toJSON(), navigator.userAgent.slice(0, 180));
  return { ok: true };
}

export async function disableBrowserPush(): Promise<void> {
  const reg = await navigator.serviceWorker.ready;
  const sub = await reg.pushManager.getSubscription();
  if (sub) { await api.pushUnsubscribe(sub.endpoint); await sub.unsubscribe(); }
}
