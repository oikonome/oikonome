// The one query client + its disk persister. Lives outside the React
// tree so the session layer can wipe, freeze and thaw it: cached
// financial data must not survive a sign-out, leak across accounts on a
// shared device, sit readable on disk, or sit in JS memory while the
// biometric gate is closed.
import AsyncStorage from "@react-native-async-storage/async-storage";
import { createAsyncStoragePersister } from "@tanstack/query-async-storage-persister";
import { QueryClient } from "@tanstack/react-query";
import { persistQueryClientRestore } from "@tanstack/react-query-persist-client";
import * as Crypto from "expo-crypto";
import * as SecureStore from "expo-secure-store";
import { MMKV } from "react-native-mmkv";

import { bindEpochSignal } from "./api";
import { resetLaunch } from "./launch";

export const PERSIST_KEY = "oikonome.queryCache";
// bump when a cached payload's SHAPE changes: a persisted pre-upgrade
// payload replayed into code that reads a field it does not carry
// crashes the app on launch
export const PERSIST_BUSTER = "payload-v2";
const GC_TIME = 1000 * 60 * 60 * 24 * 7;

// the persisted cache IS the offline read layer: last-fetched pages stay
// readable with a staleness banner; gcTime must outlive the persister
export const queryClient = new QueryClient({
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
// from travelling.
const KEY_CACHE_KEY = "oikonome.cacheKey";
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
        await SecureStore.setItemAsync(KEY_CACHE_KEY, key);
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

export const persister = createAsyncStoragePersister({
  storage, key: PERSIST_KEY });

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
