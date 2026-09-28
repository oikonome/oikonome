// Who is looking — one answer, one posture, for every gated control.
//
// Written inline, the gate has two spellings and they disagree:
// `me.data?.role !== "viewer"` reads TRUE while /api/me is still in flight
// (fail OPEN — a viewer briefly sees owner controls, clicks one, and gets a
// global 403 toast for their trouble), while `me.data ? … : false` reads
// FALSE (fail CLOSED — the control appears a moment later, which is what a
// gate should do). Two spellings across the SPA means the same person gets
// different behaviour depending which page they land on, and the native
// app deciding separately means a third answer.
//
// The rule, stated once: UNKNOWN IS NOT PRIVILEGED. Until the server says
// who you are, you are treated as the least-privileged reader.
//
// There are three roles, and two different questions to ask about them:
//
//   canEdit  — owner or member. Gates the editing chrome: saving a bill,
//              recategorising, running a sync, everything that changes the
//              household's money. Most gated controls want this one.
//   isOwner  — the owner alone. Gates the ACCOUNT: bank connections,
//              exports and restores, the household roster, billing,
//              deleting the account. A member is refused these by the
//              server, so a control shown to them would only 403.
//
// Asking the wrong one is a real bug in both directions: `isOwner` on an
// editing control hides it from the person the member role exists for,
// and `canEdit` on an owner control shows a member a button that cannot
// work. When adding a gate, look up the door in server/oikonome/web/
// permissions.py — that module is the policy both clients render.
//
// Takes the query object rather than the row so the call site cannot
// accidentally re-introduce the fail-open optional chain.

type MeRow = { role?: string | null } | undefined | null;
type MeQuery = { data?: MeRow };

/** True only once the server has confirmed someone who may change data. */
export function canEdit(me: MeQuery): boolean {
  return me?.data ? me.data.role !== "viewer" : false;
}

/** True only once the server has confirmed the account owner. */
export function isOwner(me: MeQuery): boolean {
  return me?.data ? me.data.role === "owner" : false;
}

/** True while loading OR when confirmed view-only — the safe side. */
export function isViewer(me: MeQuery): boolean {
  return !canEdit(me);
}
