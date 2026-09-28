// The one query client + its disk persister. Lives outside the React
// tree so the session layer can wipe, freeze and thaw it: cached
// financial data must not survive a sign-out, leak across accounts on a
// shared device, sit readable on disk, or sit in JS memory while the
// biometric gate is closed.
import AsyncStorage from "@react-native-async-storage/async-storage";
import { createAsyncStoragePersister } from "@tanstack/query-async-storage-persister";
import { persistQueryClientRestore } from "@tanstack/react-query-persist-client";
import * as Crypto from "expo-crypto";
import * as SecureStore from "expo-secure-store";
import { Platform } from "react-native";
import { MMKV } from "react-native-mmkv";

import { bindEpochSignal } from "./api";
import { resetLaunch } from "./launch";
import { AppQueryClient } from "./shown";
import { DEVICE_ONLY } from "./storage";

export const PERSIST_KEY = "oikonome.queryCache";
// bump when a cached payload's SHAPE changes: a persisted pre-upgrade
// payload replayed into code that reads a field it does not carry
// crashes the app on launch, and a write's patch that walks the old
// shape throws after the server has already applied the write.
// v3: builds before the OFFLINE_KEYS filter persisted every query, so a
// phone updating from one carries entries whose shape has since moved
// (the matched-pairs list became paged: {pairs} → {pages}); the whole
// old copy goes, one offline-less launch, rather than a list of shapes
// someone has to keep complete.
export const PERSIST_BUSTER = "payload-v3";
const GC_TIME = 1000 * 60 * 60 * 24 * 7;

// the persisted cache IS the offline read layer: last-fetched pages stay
// readable with a staleness banner; gcTime must outlive the persister.
// AppQueryClient: see shown.ts — deferred invalidations are announced to
// the screens that show the key.
export const queryClient = new AppQueryClient({
  defaultOptions: { queries: {
    staleTime: 60_000,
    gcTime: GC_TIME,
    retry: 1,
  } },
});

// ---- the encrypted disk copy ----
// The ledger, accounts, net worth and emails must not sit in a plain
// AsyncStorage JSON blob: that is readable by anything that reaches the
// app's files — a device backup, adb, a copy off an unlocked phone. They
// live in an MMKV file encrypted with a random key that itself sits in
// the platform secure enclave (Keychain / Keystore). The key is
// deliberately NOT biometric-gated: the persister has to open the file
// at launch, before the gate; what the gate protects is what is IN
// MEMORY (see freeze / thaw below), and the enclave keeps the disk copy
// from travelling — with DEVICE_ONLY, which is what keeps the key out of
// an iOS backup (the Keychain default is backed up).
const KEY_CACHE_KEY = "oikonome.cacheKey";
const KEY_CACHE_KEY_DEVICE_ONLY = "oikonome.cacheKey.deviceOnly";
let storePromise: Promise<MMKV> | null = null;

function encryptedStore(): Promise<MMKV> {
  if (!storePromise) {
    storePromise = (async () => {
      let key: string | null = null;
      try { key = await SecureStore.getItemAsync(KEY_CACHE_KEY); }
      catch { /* treated as absent → a fresh key, an empty cache */ }
      if (!key) {
        // MMKV keys are at most 16 bytes; 12 random bytes base64 to
        // exactly 16 ASCII characters
        const bytes = await Crypto.getRandomBytesAsync(12);
        key = btoa(String.fromCharCode(...bytes));
        await SecureStore.setItemAsync(KEY_CACHE_KEY, key, DEVICE_ONLY);
      } else if (Platform.OS === "ios") {
        // a key written before DEVICE_ONLY travels in a phone backup with
        // the ciphertext beside it; re-write it once (delete-then-add — an
        // update keeps the old accessibility)
        try {
          if ((await SecureStore.getItemAsync(KEY_CACHE_KEY_DEVICE_ONLY)) !== "1") {
            await SecureStore.deleteItemAsync(KEY_CACHE_KEY);
            await SecureStore.setItemAsync(KEY_CACHE_KEY, key, DEVICE_ONLY);
            await SecureStore.setItemAsync(KEY_CACHE_KEY_DEVICE_ONLY, "1", DEVICE_ONLY);
          }
        } catch { /* retried next launch; the key in memory still opens the cache */ }
      }
      // the pre-encryption blob, if this install ever wrote one, is
      // plaintext on disk — remove it once, best effort
      AsyncStorage.removeItem(PERSIST_KEY).catch(() => {});
      return new MMKV({ id: "oikonome-cache", encryptionKey: key });
    })();
  }
  return storePromise;
}

// ---- freeze / thaw ----
// While the gate is closed nothing from the cache may sit in JS memory,
// but the encrypted disk copy must survive the lock (it is the offline
// read layer for the next unlock). "Frozen" means: reads from disk
// return nothing (so a launch that lands on the lock screen hydrates
// nothing) and writes are dropped (so clearing memory on lock does not
// persist an empty cache over the good one). The app starts frozen and
// thaws the moment the session is ready.
let frozen = true;

// ---- request epoch ----
// Clearing the cache does not cancel an in-flight fetch: a response
// issued under the previous account could land after a sign-out and its
// onSuccess would write that household's data into the cache the next
// account is already reading. Every client fetch binds to the epoch's
// AbortSignal; a wipe (and the biometric lock) aborts the epoch, so a
// stale response can never reach the cache.
let epochController = new AbortController();

export function cacheEpochSignal(): AbortSignal {
  return epochController.signal;
}
bindEpochSignal(cacheEpochSignal);

function abortEpoch(): void {
  epochController.abort();
  epochController = new AbortController();
}

const storage = {
  getItem: async (k: string) =>
    frozen ? null : ((await encryptedStore()).getString(k) ?? null),
  setItem: async (k: string, v: string) => {
    if (frozen) return;
    (await encryptedStore()).set(k, v);
  },
  removeItem: async (k: string) => { (await encryptedStore()).delete(k); },
};

// Every cache event (a fetch settling, a row patched, a poll ticking)
// asks the persister to serialise the WHOLE client and encrypt it to
// disk. So the disk copy holds only what the next launch reads before
// the network answers — the tabs' last-known pages — and never the
// polled, searched or paged-through data that a week of gcTime would
// otherwise accumulate into a multi-megabyte blob written every second
// while a sync runs.
const OFFLINE_KEYS = new Set(["today", "bills", "accounts", "alerts", "me",
                              "settings", "onboarding", "connections",
                              "calendar", "categories", "report", "holdings",
                              "account-links", "assistant-status", "reimburse"]);
export const persistOptions = {
  persister: undefined as unknown as ReturnType<typeof createAsyncStoragePersister>,
  buster: PERSIST_BUSTER,
  maxAge: GC_TIME,
  dehydrateOptions: {
    shouldDehydrateQuery: (q: { queryKey: readonly unknown[];
                                state: { status: string } }) =>
      q.state.status === "success"
      && (OFFLINE_KEYS.has(String(q.queryKey[0]))
          // the month a person lands on, not every month or search
          // they paged through
          || (q.queryKey[0] === "transactions" && q.queryKey.length > 1
              && isLandingPage(q.queryKey[1]))),
  },
};

/** the Transactions tab's opening page: the current month, no search,
 *  no bucket — the shape `transactions.tsx` builds before anyone types
 *  or pages back (every month browsed with ‹ › matches the filter-free
 *  shape too, and a week of them is not the offline layer) */
function isLandingPage(params: unknown): boolean {
  if (!params || typeof params !== "object") return false;
  const p = params as Record<string, unknown>;
  if (p.q || p.page || p.bucket || p.acct || p.cat) return false;
  const now = new Date();
  return Number(p.y) === now.getFullYear() && Number(p.m) === now.getMonth() + 1;
}

export const persister = createAsyncStoragePersister({
  storage, key: PERSIST_KEY,
  // one write a few seconds after the last change, not one a second
  throttleTime: 5000 });
persistOptions.persister = persister;

/** Open the encrypted store early (the enclave key read + MMKV open are
 *  the slow part) so the restore after the biometric prompt is only a
 *  parse. Nothing is read: the store stays frozen until the unlock. */
export function warmQueryStore(): void {
  void encryptedStore().catch(() => {});
}

/** The gate closed: drop every cached query from memory, keep the
 *  encrypted disk copy. */
export function lockQueryCache(): void {
  frozen = true;
  // Deliberately NO abortEpoch() here: the lock is the SAME account and
  // the same session, and aborting a mid-flight save would tell the user
  // it failed when the server may have applied it (backgrounding >15s
  // during a slow write is an ordinary event). The epoch abort exists for
  // the cross-ACCOUNT leak, which only a wipe (sign-out/switch) opens; a
  // late same-session response landing in cleared memory here is data the
  // gate's owner already owns, and the freeze keeps it off disk.
  queryClient.clear();
}

/** The gate opened: bring the disk copy back into memory. */
export async function unlockQueryCache(): Promise<void> {
  frozen = false;
  try {
    await persistQueryClientRestore({ queryClient, persister,
      buster: PERSIST_BUSTER, maxAge: GC_TIME });
  } catch { /* an unreadable cache is discarded by the restore itself */ }
}

/** Forget every cached query, in memory and on disk. Called on sign-out,
 *  auth loss, and right before a fresh login completes. */
export async function wipeQueryCache(): Promise<void> {
  // freeze first: the persister debounces writes, and a flush scheduled
  // before the wipe would otherwise re-write the old household's data to
  // disk after it — the next unlock thaws again
  frozen = true;
  abortEpoch();
  resetLaunch();   // the next household on this launch gets its own home
  queryClient.clear();
  try { await persister.removeClient(); } catch { /* best effort */ }
  try { await AsyncStorage.removeItem(PERSIST_KEY); } catch { /* best effort */ }
}
