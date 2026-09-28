// The merchant's mark beside its name — mirrors the web's MerchantAvatar:
// Plaid's logo when the merchant has one, else a monogram in a hue that is
// stable for that merchant. One component so every screen draws it the same
// way and the "merchant logos" setting (Settings → Merchants; default
// on) turns them all off at once.
import { Image } from "expo-image";
import { createContext, useContext, useState } from "react";
import { Text, View } from "react-native";

import { useSession } from "../lib/session";
import { hueOf, logoProxyUrl, monogram } from "../lib/pure";
import { useMe } from "../lib/viewer";

/** the household's logo switch (from /api/me; default on). One avatar
 *  per ledger row means hundreds of them at once, so the switch is read
 *  ONCE, by the provider at the root, and handed down: a row is never a
 *  query observer of its own, and a /api/me refetch notifies one
 *  subscriber instead of every visible row. */
const LogosContext = createContext<boolean>(true);

export function LogosProvider({ children }: { children: React.ReactNode }) {
  const me = useMe();
  return (
    <LogosContext.Provider value={me.data?.merchant_logos !== false}>
      {children}
    </LogosContext.Provider>
  );
}

export function useLogosOn(): boolean {
  return useContext(LogosContext);
}

export default function MerchantAvatar({ name, logo, size = 30 }:
    { name: string; logo?: string | null; size?: number }) {
  const on = useLogosOn();
  const { client } = useSession();
  // the logo URL that failed to load — remembered by value, not as a bare
  // flag, so a recycled row showing a different merchant tries again
  const [failed, setFailed] = useState<string | null>(null);
  if (!on) return null;
  const r = Math.round(size / 4);
  // through the instance's own cache (/api/logo), never plaid.com direct —
  // and only for URLs the cache will actually serve
  const uri = client && failed !== logo
    ? logoProxyUrl(client.baseUrl, logo) : null;
  if (uri) {
    // /api/logo needs the session; the phone holds a bearer token, not a
    // cookie, so the image request carries it itself
    // cached in memory and on disk across recycled cells — a mark seen
    // once on this phone is never fetched again for the same URL
    return <Image source={{ uri, headers: client!.authHeader }}
                  cachePolicy="memory-disk" recyclingKey={uri}
                  onError={() => setFailed(logo ?? null)}
                  style={{ width: size, height: size, borderRadius: r,
                           backgroundColor: "#fff" }} contentFit="contain" />;
  }
  return (
    <View style={{ width: size, height: size, borderRadius: r,
                   backgroundColor: `hsl(${hueOf(name)}, 40%, 30%)`,
                   alignItems: "center", justifyContent: "center" }}>
      <Text style={{ color: "#fff", fontWeight: "700",
                     fontSize: Math.max(9, Math.round(size * 0.42)) }}>
        {monogram(name)}
      </Text>
    </View>
  );
}
