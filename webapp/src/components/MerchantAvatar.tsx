// The merchant's mark beside its name: Plaid's logo when the merchant has
// one, else a monogram in a hue that is stable for that merchant. One
// component so every surface (ledger, merchants, spending, bill history,
// Today) draws it the same way — and so the "merchant logos" setting can
// turn them ALL off from one place (Settings → Merchants; default on).
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { api, logoProxyUrl } from "../api/client";

/** stable 0-359 hue from a string — same merchant, same colour, every render */
export function hueOf(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h % 360;
}

/** "Trader Joe's" → "TJ", "Bigbox" → "Bi", "7-Eleven" → "7E" */
export function monogram(name: string): string {
  const words = (name || "").replace(/[^A-Za-z0-9 ]+/g, " ").trim().split(/\s+/)
    .filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2);
  return (words[0][0] + words[1][0]).toUpperCase();
}

/** the household's logo switch (Settings → merchant logos; default on) */
export function useLogosOn(): boolean {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me, staleTime: 60_000 });
  return me.data?.merchant_logos !== false;
}

export default function MerchantAvatar({ name, logo, size = 20, style }: {
  name: string; logo?: string | null; size?: number;
  style?: React.CSSProperties;
}) {
  const on = useLogosOn();
  // the logo URL that failed to load — remembered by value, not as a bare
  // flag, so a recycled row showing a different merchant tries again
  const [failed, setFailed] = useState<string | null>(null);
  if (!on) return null;
  const r = Math.round(size / 4);
  // through the instance's own cache (/api/logo), never plaid.com direct —
  // and only for URLs the cache will actually serve
  const src = logo && failed !== logo ? logoProxyUrl(logo) : null;
  if (src) {
    return (
      // no crossOrigin: /api/logo is same-origin and needs the session
      // cookie, which an anonymous request would drop
      <img src={src} alt="" width={size} height={size} loading="lazy"
           onError={() => setFailed(logo ?? null)}
           style={{ width: size, height: size, borderRadius: r,
                    background: "#fff", objectFit: "contain",
                    verticalAlign: "middle", flex: "0 0 auto", ...style }} />
    );
  }
  return (
    <span aria-hidden="true"
          style={{ display: "inline-flex", alignItems: "center",
                   justifyContent: "center", width: size, height: size,
                   borderRadius: r, flex: "0 0 auto",
                   background: `hsl(${hueOf(name)} 40% 30%)`, color: "#fff",
                   fontSize: Math.max(9, Math.round(size * 0.48)),
                   fontWeight: 700, lineHeight: 1, verticalAlign: "middle",
                   ...style }}>
      {monogram(name)}
    </span>
  );
}
