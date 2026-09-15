// The merchant's mark beside its name — mirrors the web's MerchantAvatar:
// Plaid's logo when the merchant has one, else a monogram in a hue that is
// stable for that merchant. One component so every screen draws it the same
// way and the "merchant logos" setting (Settings → Merchants; default
// on) turns them all off at once.
import { useState } from "react";
import { Image, Text, View } from "react-native";

import { useSession } from "../lib/session";
import { hueOf, logoProxyUrl, monogram } from "../lib/pure";
import { useMe } from "../lib/viewer";

/** the household's logo switch (from /api/me; default on). One avatar
 *  per ledger row means hundreds of these observers at once — they share
 *  the viewer's /api/me query and its staleTime, so mounting a list never
 *  triggers a refetch of its own. */
export function useLogosOn(): boolean {
  const me = useMe();
  return me.data?.merchant_logos !== false;
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
    return <Image source={{ uri, headers: client!.authHeader }}
                  accessibilityIgnoresInvertColors
                  onError={() => setFailed(logo ?? null)}
                  style={{ width: size, height: size, borderRadius: r,
                           backgroundColor: "#fff" }} resizeMode="contain" />;
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
