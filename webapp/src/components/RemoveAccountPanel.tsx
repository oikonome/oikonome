// The account-kind vocabulary and the remove-an-account panel, shared by
// the Accounts page and the Manage-accounts card in Settings. A module of
// its own so the card does not import the whole Accounts page for them.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, errText, type Account, type AccountRemoveMode,
         type AccountRemoveResult, type Connection }
  from "../api/client";
import { patchList, removeFromList } from "../api/cache";
import { sharedLock } from "../sharedlogin";

export const KINDS: [string, string][] = [
  ["depository/checking", "checking"],
  ["depository/savings", "savings"],
  ["credit/credit card", "credit card"],
  ["investment/brokerage", "investment"],
  ["loan/", "loan"],
];

// What a write can have moved. Every editor on this page reports back
// through one `onDone(msg, moved)`; the page refetches only those keys.
// Omitting `moved` means "everything" — the paths that link or disconnect a
// bank really do change all of it.
//
// "me" is in here because /api/me carries the institution allowance — how
// many of this account's institutions are spent — and every connect and disconnect
// moves it. Leaving it out is invisible until it matters: a household at
// its institution limit disconnects a bank to make room and the Connect button stays
// disabled, behind a count cached from before the very change that freed
// the slot.
export type Moved = "accounts" | "settings" | "connections" | "today"
  | "calendar" | "me";
export type DoneFn = (msg: string, moved?: Moved[]) => void;
type AccountsData = { accounts: Account[] };

/** The keys a completed removal can have moved. A disconnect archives its
 *  institution and a purge deletes what is left of one, so both change the
 *  allowance on /api/me; hiding an account changes nothing the server
 *  counts. Mobile's twin is `accountRemoveMoved` in mobile/src/lib/pure.ts
 *  — the same rule, over the key names that client uses (its ledger is
 *  ["transactions"], this one's is ["txns"] and is not mounted here). */
/** What to say after a removal. A purge that kept rows (the bank feed
 *  already relies on them) explains so in the purge's own note, beside
 *  the removal's note; the same sentence is never said twice. */
export function accountRemoveNote(r: AccountRemoveResult): string {
  const parts: string[] = [];
  if (r.note) parts.push(r.note);
  const kept = r.purged?.note;
  if (kept && !(r.note ?? "").includes(kept)) parts.push(kept);
  return (parts.join(" ") || "Removed.")
    + (r.plaid_released ? " Plaid connection released." : "");
}

export function accountRemoveMoved(mode: AccountRemoveMode): Moved[] {
  const keys: Moved[] = ["accounts", "today"];
  if (mode !== "hide") keys.push("connections", "me");
  // a purge can take the checking anchor or an excluded account with it,
  // and the bill calendar's balances — the settings view and the calendar
  // are the server's to recompute
  if (mode.endsWith("purge")) keys.push("settings", "calendar");
  return keys;
}

/** The question a delete refused with "kept_rows_unconfirmed" asks, or
 *  null for any other error. The server keeps imported rows a bank feed
 *  may have set its own copy aside for; when every kept row is held only
 *  by a note that names no feed account, nothing would ever release them,
 *  so it asks the owner instead and "delete anyway" re-sends with
 *  `force_kept`. Mobile's twin is `keptRowsQuestion` in
 *  mobile/src/lib/pure.ts. */
export function keptRowsQuestion(e: unknown): string | null {
  const m = /^409:\s*([\s\S]*)$/.exec(e instanceof Error ? e.message : "");
  if (!m) return null;
  try {
    const b = JSON.parse(m[1]);
    return b?.error === "kept_rows_unconfirmed" && typeof b.detail === "string"
      ? b.detail : null;
  } catch { return null; }
}

/** The note an undo refused with "kept_rows" carries: a member's undo
 *  that would leave such rows is told why and offered nothing, because
 *  deleting them anyway is the owner's call. Null for any other error.
 *  Mobile's twin is `keptRowsNote` in mobile/src/lib/pure.ts. */
export function keptRowsNote(e: unknown): string | null {
  const m = /^409:\s*([\s\S]*)$/.exec(e instanceof Error ? e.message : "");
  if (!m) return null;
  try {
    const b = JSON.parse(m[1]);
    return b?.error === "kept_rows" && typeof b.detail === "string" && b.detail
      ? b.detail : null;
  } catch { return null; }
}

export function RemoveAccountPanel({ a, liveConn, onDone, onCancel }: {
  a: Account; liveConn: boolean;
  onDone: DoneFn; onCancel: () => void;
}) {
  const qc = useQueryClient();
  const isPlaid = a.aggregator === "plaid";
  const inst = a.institution_name || "this institution";
  // Disconnecting ends a bank connection that outlives the shared
  // demonstration login (a Plaid release cannot be undone), so the server
  // refuses both disconnect modes to it; they are shown switched off with
  // the reason, and hiding the one account stays open.
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const credLock = liveConn ? sharedLock(meQ.data, "credential") : null;
  // at the allowance, the likely reason to disconnect is to make room —
  // and a released bank keeps its place for 30 days, so say that first
  const cap = meQ.data?.features?.institution_cap;
  const used = meQ.data?.features?.institutions_used;
  const atCap = liveConn && isPlaid && typeof cap === "number"
    && typeof used === "number" && used >= cap;
  // default: live → disconnect keep history; dead/manual → purge
  const [mode, setMode] = useState<AccountRemoveMode>(
    liveConn ? "disconnect" : "purge");
  const lockedMode = (m: AccountRemoveMode) =>
    !!credLock && m.startsWith("disconnect");
  // the lock can arrive after the first render (me loads late), so the
  // chosen mode follows it rather than the initial default
  const effMode: AccountRemoveMode = lockedMode(mode) ? "hide" : mode;

  const remove = useMutation({
    mutationFn: async () => {
      try {
        return await api.accountRemove(a.id, effMode);
      } catch (e) {
        // rows only a note naming no feed account keeps: the server asks
        // before deleting them, and the answer re-sends the same removal
        const q = keptRowsQuestion(e);
        if (q && window.confirm(`${q}\n\nDelete anyway?`))
          return await api.accountRemove(a.id, effMode, true);
        throw e;
      }
    },
    onSuccess: (r) => {
      // The server names every account it touched. Purged rows leave the
      // list now; a hidden one moves to its section; a disconnected
      // institution shows archived on every row — all before the refetch,
      // which only confirms.
      const hit = new Set(r.affected_accounts);
      // a purge that kept rows keeps their account too; the refetch says which
      const kept = (r.purged?.kept_transactions ?? 0) > 0;
      if ((r.mode === "purge" || r.mode === "disconnect_purge") && !kept)
        removeFromList<AccountsData, Account>(qc, ["accounts"], "accounts",
          (x) => hit.has(x.id));
      else if (!kept)
        patchList<AccountsData, Account>(qc, ["accounts"], "accounts",
          (x) => hit.has(x.id),
          r.mode === "hide"
            ? { user_removed_at: new Date().toISOString(),
                balance_current: null }
            : { status: "archived" });
      // the connection list no longer serves a disconnected item
      if (r.mode.startsWith("disconnect") && r.item_id) {
        removeFromList<{ connections: Connection[] }, Connection>(
          qc, ["connections"], "connections", (c) => c.id === r.item_id);
        // The allowance is NOT patched down here: a released Item keeps
        // holding its place for a while, so the count may not move at
        // all. The "me" refetch below brings the server's own figure.
      }
      onDone(accountRemoveNote(r), accountRemoveMoved(r.mode));
    },
    onError: (e) => onDone(errText(e)),
  });

  const opts: { mode: AccountRemoveMode; label: string; detail: string }[] = [];
  if (liveConn) {
    opts.push({
      mode: "disconnect",
      label: "Disconnect only — keep history",
      detail: isPlaid
        ? `Stops syncing ${inst} and releases the Plaid Item (stops billing). `
          + "Transaction history stays. Affects every account from this bank."
        : `Stops syncing ${inst}. History stays. Affects every account from this bank.`,
    });
    opts.push({
      mode: "disconnect_purge",
      label: "Disconnect and delete data",
      detail: isPlaid
        ? `Releases Plaid, stops syncing ${inst}, and permanently deletes all `
          + "local accounts and transactions for this institution."
        : `Stops syncing ${inst} and permanently deletes all local accounts `
          + "and transactions for this institution.",
    });
    opts.push({
      mode: "hide",
      label: "Hide this account only",
      detail: "Removes this account from active lists but keeps the bank "
        + "connection (and other accounts) syncing. History kept. Does not "
        + "free a Plaid slot.",
    });
  } else {
    opts.push({
      mode: "purge",
      label: "Delete account and its transactions",
      detail: "Permanently deletes this account and its transaction history. "
        + "Cannot be undone.",
    });
    if (!a.user_removed_at) {
      opts.push({
        mode: "hide",
        label: "Hide this account",
        detail: "Move it to archived without deleting history.",
      });
    }
  }

  return (
    <div style={{ marginTop: ".55rem", padding: ".65rem .75rem",
                  border: "1px solid var(--line)", borderRadius: "var(--rs)",
                  maxWidth: "36rem", fontSize: 13 }}>
      <div style={{ fontWeight: 600, marginBottom: ".35rem" }}>
        Remove {a.name}{a.mask ? ` ${a.mask}` : ""}?</div>
      <div className="mut" style={{ marginBottom: ".5rem" }}>
        {liveConn
          ? "Bank connections are per institution — disconnect releases the "
            + "whole login (Plaid Item), not a single sub-account."
          : "This account is not actively syncing."}
      </div>
      {atCap && (
        <div style={{ color: "var(--amber)", marginBottom: ".5rem" }}>
          Disconnecting does not free a slot for 30 days.</div>
      )}
      {credLock && (
        <div style={{ marginBottom: ".5rem", color: "var(--amber)" }}>
          {credLock}</div>
      )}
      {opts.map((o) => (
        <label key={o.mode}
               style={{ display: "block", marginBottom: ".45rem",
                        cursor: lockedMode(o.mode) ? "not-allowed" : "pointer",
                        opacity: lockedMode(o.mode) ? .55 : 1 }}>
          <input type="radio" name={`rm-${a.id}`} value={o.mode}
                 checked={effMode === o.mode}
                 disabled={lockedMode(o.mode)}
                 onChange={() => setMode(o.mode)}
                 style={{ marginRight: ".4rem" }} />
          <b>{o.label}</b>
          <div className="mut" style={{ marginLeft: "1.3rem", fontSize: 12 }}>
            {o.detail}</div>
        </label>
      ))}
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".5rem" }}>
        <button type="button" className="pri"
                style={{ background: "var(--red, #c44)", borderColor: "transparent" }}
                disabled={remove.isPending}
                onClick={() => {
                  const need = effMode === "disconnect_purge" || effMode === "purge";
                  const msg = effMode === "disconnect_purge"
                    ? `Disconnect ${inst} and DELETE all its local data? This cannot be undone.`
                    : effMode === "purge"
                      ? `Permanently delete ${a.name} and its transactions?`
                      : effMode === "disconnect"
                        ? `Disconnect ${inst}? Syncing stops; history is kept.`
                        : `Hide ${a.name} from active accounts?`;
                  if (need ? window.confirm(msg) : window.confirm(msg))
                    remove.mutate();
                }}>
          {remove.isPending ? "removing…" : "confirm remove"}
        </button>
        <button type="button" className="btn" onClick={onCancel}>cancel</button>
      </div>
    </div>
  );
}
