// Browser (Web Push) registration, shared by Settings and the setup
// wizard so the two doors cannot drift: same permission handling, same
// subscription shape, same reasons when it cannot happen.
import { api } from "./api/client";

/** Notifications need a secure context and a service worker; on http://
 *  Chrome resolves requestPermission() "denied" without ever prompting,
 *  so the origin has to be tested BEFORE asking. */
export function browserPushBlocked(): boolean {
  return typeof window !== "undefined"
    && (!window.isSecureContext || !("serviceWorker" in navigator));
}

/** This browser's own subscription endpoint, or undefined — bounded, so
 *  a worker that never becomes ready cannot hang the caller. */
export async function ownPushEndpoint(): Promise<string | undefined> {
  if (!("serviceWorker" in navigator)) return undefined;
  try {
    const reg = await Promise.race([
      navigator.serviceWorker.ready,
      new Promise<null>((res) => setTimeout(() => res(null), 1500)),
    ]);
    if (!reg) return undefined;
    return (await reg.pushManager.getSubscription())?.endpoint;
  } catch { return undefined; }
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
