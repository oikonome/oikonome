// Who is asking, as far as the UI is concerned. Both hooks read the SAME
// cached /api/me query, so asking for one costs nothing extra.
import { useQuery } from "@tanstack/react-query";

import { useSession } from "./session";

export function useMe() {
  const { client } = useSession();
  return useQuery({
    queryKey: ["me"],
    queryFn: () => client!.me(),
    enabled: !!client,
    staleTime: 5 * 60_000,
  });
}

// There are three household roles, and two questions worth asking:
//
//   useViewer — is this person unable to change anything? Gates the
//               editing chrome. A member answers false, like the owner.
//   useOwner  — is this the account owner? Gates the ACCOUNT: bank
//               connections, exports and restores, the household roster,
//               billing, deleting the account. The server refuses these
//               for a member, so a control shown to one only 403s.
//
// Both fail CLOSED while /api/me is in flight, for the reason the web's
// role.ts states at length: a briefly-missing button beats a button that
// breaks when tapped.
//
// Viewer-role gate: a viewer sees everything and changes nothing. The
// web disables its controls; the app hides them — a control that only
// 403s is a broken-looking button.
export function useViewer(): boolean {
  const me = useMe();
  // Unknown fails CLOSED: until /api/me answers (the first render after
  // login wipes the cache), report "viewer" so write controls stay
  // hidden — briefly missing a button beats showing a viewer one that
  // 403s the moment they tap it.
  return me.data ? me.data.role === "viewer" : true;
}

// A demo instance refuses every settings write (its credentials are shared
// and printed publicly), so a control whose only effect is a settings write
// flips and snaps back there. Callers use this to keep view-only
// preferences per-device on a demo instead of dead-ending on a 403.
// Unknown reports false — the ordinary instance is the common case, and
// guessing "demo" would silently stop a real account from saving. That is
// safe against the load window because every caller's control is ALSO
// gated on useViewer, which fails closed on the same query: nothing is
// tappable until /api/me has answered and both hooks know the truth.
export function useDemo(): boolean {
  const me = useMe();
  return !!me.data?.demo;
}

// The account owner. Mirrors `isOwner` in webapp/src/role.ts; the policy
// both clients render lives in server/oikonome/web/permissions.py.
export function useOwner(): boolean {
  const me = useMe();
  return me.data ? me.data.role === "owner" : false;
}
