// The home-screen widget's phone-side plumbing: its credential, its
// cache, and the refresh the app triggers when it learns something new.
//
// The widget cannot use the device token — that one sits behind the
// biometric gate on purpose, and a widget redraws on the OS's clock with
// nobody holding the phone. It polls with its own lesser token (minted
// off the device row by POST /api/devices/widget; it opens exactly one
// door and dies with the device). That token and the server URL live in
// the plain secure-store home, readable without a prompt:
//   Android — the Keystore-backed preferences, read by the headless
//             widget task in the app's own process.
//   iOS     — the Keychain, in the app group shared with the widget
//             extension (targets/glance), which reads them from Swift.
// The cache is the last payload plus when it was fetched, so a redraw
// with no network still shows something honest (grey, with its time),
// stamped with the credential it was fetched under: it is read back only
// while that credential is still the widget's.
import Constants from "expo-constants";
import * as SecureStore from "expo-secure-store";

import { queryClient } from "./query";
import { Platform } from "react-native";

import type { Client } from "./api";
import { credentialStamp, type Glance, type GlanceCache, ownedCache,
         shouldRefreshWidget,
         saveStillOwned, widgetOwner, widgetOwnerChanged }
  from "./glance-pure";

export const KEY_WIDGET_TOKEN = "oikonome.widgetToken";
export const KEY_WIDGET_URL = "oikonome.widgetServerUrl";
const KEY_WIDGET_CACHE = "oikonome.widgetGlance";
const KEY_WIDGET_OWNER = "oikonome.widgetOwner";
/** iOS: the extension's own last payload (targets/glance, `Cache`), a
 *  Keychain item in the shared access group — keep the name in step. */
const KEY_WIDGET_CACHE_IOS = "oikonome.widgetGlanceExt";

/** iOS: the app group the widget extension shares, `group.<bundle id>`
 *  (plugins/with-widget-sharing.js grants it to both). Used as the
 *  Keychain access group — app-group identifiers are accepted there
 *  without a team prefix, which is what lets a JS-side write name it. */
export function sharedAccessGroup(): string | undefined {
  if (Platform.OS !== "ios") return undefined;
  const id = Constants.expoConfig?.ios?.bundleIdentifier;
  return id ? `group.${id}` : undefined;
}

// AFTER_FIRST_UNLOCK: a widget timeline refreshes while the phone is
// locked; WHEN_UNLOCKED would make every background redraw find nothing.
// Still this-device-only, like every credential the app keeps.
const OPTS: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
  ...(sharedAccessGroup() ? { accessGroup: sharedAccessGroup() } : {}),
};

// The glance cache (the household's day: verdict, amounts, chips) and the
// owner stamp beside it are financial data, so they stay off backups too.
// They are read only by app JS — the Android widget's headless task; the iOS
// extension keeps its own copy — so no shared access group, which keeps the
// read and write queries the same as they always were.
const LOCAL_OPTS: SecureStore.SecureStoreOptions = {
  keychainAccessible: SecureStore.AFTER_FIRST_UNLOCK_THIS_DEVICE_ONLY,
};

let minted = false;
let movedLocal = false;

/** Items written before LOCAL_OPTS carry the backup-able default, and an
 *  update keeps an existing Keychain item's accessibility — so re-create
 *  them once per launch (delete, then add). Only iOS has the distinction. */
async function moveLocalToDeviceOnly() {
  if (movedLocal || Platform.OS !== "ios") return;
  movedLocal = true;
  for (const key of [KEY_WIDGET_OWNER, KEY_WIDGET_CACHE]) {
    try {
      const v = await SecureStore.getItemAsync(key);
      if (v === null) continue;
      await SecureStore.deleteItemAsync(key);
      await SecureStore.setItemAsync(key, v, LOCAL_OPTS);
    } catch { movedLocal = false; }   // tried again next launch
  }
}

/** the signed-in identity the tabs already fetched, without a second
 *  /api/me at launch; the network only when nothing is cached yet */
async function meOf(client: Client): Promise<{ tenant_id: string }> {
  const cached = queryClient.getQueryData<{ tenant_id: string }>(["me"]);
  return cached ?? client.me();
}

/** Once per sign-in: give this phone's widget its credential. Replaces
 *  whatever the server held for this device, so a token the widget lost
 *  (a reinstall, a cleared keychain) is simply issued again. Fire-and-
 *  forget like push registration: the widget is a convenience. */
export async function ensureWidgetToken(client: Client, serverUrl: string,
                                        opts: { force?: boolean } = {}) {
  if (minted && !opts.force) return;
  minted = true;
  try {
    await moveLocalToDeviceOnly();
    const me = await meOf(client);
    const owner = widgetOwner(serverUrl, me.tenant_id);
    const heldFor = await SecureStore.getItemAsync(KEY_WIDGET_OWNER);
    // a credential this phone already holds for this household on this
    // server is still good (it dies only with the device token, which is
    // this session's): every launch need not mint another
    if (!opts.force && heldFor === owner
        && await SecureStore.getItemAsync(KEY_WIDGET_TOKEN))
      return;
    const { token } = await client.widgetToken();
    // another household (or server) than the cache was fetched for: drop
    // it BEFORE the new credential lands, so no redraw can pair the two
    if (widgetOwnerChanged(heldFor, owner))
      await dropGlanceCaches();
    await SecureStore.setItemAsync(KEY_WIDGET_OWNER, owner, LOCAL_OPTS);
    await SecureStore.setItemAsync(KEY_WIDGET_TOKEN, token, OPTS);
    await SecureStore.setItemAsync(KEY_WIDGET_URL, serverUrl, OPTS);
    await refreshWidget(client);
  } catch {
    minted = false;                 // try again next launch
  }
}

/** Sign-out / lost credentials: the widget goes blank at once. The
 *  server-side token dies with the device row; this removes the copy. */
export async function clearWidget() {
  minted = false;
  await Promise.all([
    SecureStore.deleteItemAsync(KEY_WIDGET_TOKEN, OPTS),
    SecureStore.deleteItemAsync(KEY_WIDGET_URL, OPTS),
    SecureStore.deleteItemAsync(KEY_WIDGET_OWNER),
    dropGlanceCaches(),
  ]).catch(() => {});
  await redrawWidgets();
}

/** Both caches of the last glance: the JS one (the Android widget's) and
 *  the iOS extension's — a Keychain item in the shared group, plus the
 *  app group's defaults where older builds of the extension kept it. */
async function dropGlanceCaches() {
  await SecureStore.deleteItemAsync(KEY_WIDGET_CACHE).catch(() => {});
  if (Platform.OS === "ios") {
    await SecureStore.deleteItemAsync(KEY_WIDGET_CACHE_IOS, OPTS)
      .catch(() => {});
    try {
      const { clearWidgetCache } = await import("../../modules/widget-reload");
      clearWidgetCache();
    } catch { /* Expo Go, or a build without the extension */ }
  }
}

export async function readWidgetCredential():
    Promise<{ url: string; token: string } | null> {
  try {
    const [url, token] = await Promise.all([
      SecureStore.getItemAsync(KEY_WIDGET_URL, OPTS),
      SecureStore.getItemAsync(KEY_WIDGET_TOKEN, OPTS)]);
    return url && token ? { url, token } : null;
  } catch { return null; }
}

/** The last glance, if it belongs to the credential the widget holds now
 *  (see glance-pure.ownedCache). */
export async function readGlanceCache(): Promise<GlanceCache | null> {
  try {
    const [raw, cred] = await Promise.all([
      SecureStore.getItemAsync(KEY_WIDGET_CACHE), readWidgetCredential()]);
    if (!raw) return null;
    const c = JSON.parse(raw) as GlanceCache;
    return c && c.glance && typeof c.fetched_at === "number"
      ? ownedCache(c, cred?.token) : null;
  } catch { return null; }
}

/** Save a glance fetched while `token` was the widget's credential — but
 *  only if it still is. The check runs after the fetch, so a sign-out or a
 *  sign-in to another household that happened while it was in flight
 *  wins; the stamp inside the entry covers a change that lands between
 *  this check and the write. `startedAt` is when the request was sent,
 *  not when it landed: a fetch sent at 11:59 PM carries the day that was
 *  ending, so if it lands after midnight it must read as yesterday's (grey)
 *  rather than as today's allowance. Returns whether it saved. */
async function writeGlanceCache(glance: Glance, token: string,
                                startedAt: number) {
  await moveLocalToDeviceOnly();
  const now = (await readWidgetCredential())?.token;
  if (!saveStillOwned(token, now)) return false;
  const c: GlanceCache = { glance, fetched_at: startedAt,
                           owner: credentialStamp(token) };
  await SecureStore.setItemAsync(KEY_WIDGET_CACHE, JSON.stringify(c),
                              LOCAL_OPTS);
  return true;
}

/** Poll the glance door with the widget credential (the headless
 *  Android task). A 401 means the credential is gone — revoked device,
 *  password change — so the copy is dropped and the widget says "Sign
 *  in" until the app next opens and mints again. Any other failure
 *  keeps the cache: stale beats wrong, and wrong beats blank. */
export async function fetchGlanceForWidget(): Promise<GlanceCache | null> {
  const cred = await readWidgetCredential();
  if (!cred) return null;
  const startedAt = Date.now();
  try {
    const r = await fetch(cred.url + "/api/today/glance", {
      // no-store: kept out of the platform's HTTP disk cache, as in api.ts
      headers: { Authorization: `Bearer ${cred.token}`,
                 Accept: "application/json", "Cache-Control": "no-store" } });
    if (r.status === 401) {
      // the payload goes with the credential it was fetched under — but
      // only if that is still the one stored: the app may have signed in
      // again and minted a fresh credential while this request was out,
      // and the refusal says nothing about that one
      const now = (await readWidgetCredential())?.token;
      if (!saveStillOwned(cred.token, now)) return readGlanceCache();
      await SecureStore.deleteItemAsync(KEY_WIDGET_TOKEN, OPTS);
      await SecureStore.deleteItemAsync(KEY_WIDGET_CACHE);
      return null;
    }
    if (!r.ok) return readGlanceCache();
    const glance = (await r.json()) as Glance;
    // the credential changed while this was in flight: these numbers are
    // the previous credential's, so draw whatever the current one owns
    if (!(await writeGlanceCache(glance, cred.token, startedAt)))
      return readGlanceCache();
    return { glance, fetched_at: startedAt,
             owner: credentialStamp(cred.token) };
  } catch {
    return readGlanceCache();
  }
}

/** The app just learned today's numbers (the Today query settled, a
 *  category changed): refresh the cache through the signed-in client
 *  and redraw, so opening the app never leaves the widget behind. */
export async function refreshWidget(client: Client) {
  try {
    // stamp with the widget credential current when the fetch begins; no
    // credential (signed out, not minted yet) means nothing to save for
    // Only Android reads this cache: the iOS extension fetches the glance
    // itself when its timeline reloads, so fetching here too would run the
    // server's Today computation a second time for nothing.
    const cred = Platform.OS === "android" ? await readWidgetCredential() : null;
    if (cred) {
      const startedAt = Date.now();
      await writeGlanceCache(await client.glance(), cred.token, startedAt);
    }
  } catch { /* the widget keeps what it has */ }
  await redrawWidgets();
}

/** How many of this app's widgets sit on the home screen, or null when the
 *  platform cannot say (Expo Go, a build whose native module predates the
 *  count). */
export async function placedWidgetCount(): Promise<number | null> {
  try {
    if (Platform.OS === "android") {
      const { getWidgetInfo } = await import("react-native-android-widget");
      const { WIDGET_NAME } = await import("../widgets/android");
      return (await getWidgetInfo(WIDGET_NAME)).length;
    }
    if (Platform.OS === "ios") {
      const { placedWidgets } = await import("../../modules/widget-reload");
      return await placedWidgets();
    }
  } catch { /* fall through: unknown */ }
  return null;
}

// the last refresh this process made, for shouldRefreshWidget
let lastRefresh: { stamp: string; at: number } | null = null;

/** The Today screen's hook: refresh the widget only when one is placed
 *  (or, when that cannot be known, at most every fifteen minutes and only
 *  when Today changed). */
export async function refreshWidgetIfPlaced(client: Client, stamp: string) {
  const placed = await placedWidgetCount();
  const now = Date.now();
  if (!shouldRefreshWidget(placed, stamp, lastRefresh, now)) return;
  lastRefresh = { stamp, at: now };
  await refreshWidget(client);
}

/** Ask the OS to redraw every placed widget from the cache. Android
 *  renders here (the widget is a JSX tree the library rasterises); iOS
 *  reloads the WidgetKit timeline, and the extension fetches for itself. */
export async function redrawWidgets() {
  try {
    if (Platform.OS === "android") {
      const { redrawAndroidWidgets } = await import("../widgets/android");
      await redrawAndroidWidgets();
    } else if (Platform.OS === "ios") {
      const { reloadWidgets } = await import("../../modules/widget-reload");
      reloadWidgets();
    }
  } catch { /* Expo Go, or no widget placed */ }
}
