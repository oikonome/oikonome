// The account-kind vocabulary and the remove-an-account panel, shared by
// the Accounts page and the Manage-accounts card in Settings. A module of
// its own so the card does not import the whole Accounts page for them.
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, errText, type Account, type AccountRemoveMode,
         type Connection, type Me } from "../api/client";
import { patchList, patchQuery, removeFromList } from "../api/cache";

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
export function accountRemoveMoved(mode: AccountRemoveMode): Moved[] {
  const keys: Moved[] = ["accounts", "today"];
  if (mode !== "hide") keys.push("connections", "me");
  // a purge can take the checking anchor or an excluded account with it,
  // and the bill calendar's balances — the settings view and the calendar
  // are the server's to recompute
  if (mode.endsWith("purge")) keys.push("settings", "calendar");
  return keys;
}

export function RemoveAccountPanel({ a, liveConn, onDone, onCancel }: {
  a: Account; liveConn: boolean;
  onDone: DoneFn; onCancel: () => void;
}) {
  const qc = useQueryClient();
  const isPlaid = a.aggregator === "plaid";
  const inst = a.institution_name || "this institution";
  // default: live → disconnect keep history; dead/manual → purge
  const [mode, setMode] = useState<AccountRemoveMode>(
    liveConn ? "disconnect" : "purge");

  const remove = useMutation({
    mutationFn: () => api.accountRemove(a.id, mode),
    onSuccess: (r) => {
      const extra = r.plaid_released ? " Plaid connection released." : "";
      // The server names every account it touched. Purged rows leave the
      // list now; a hidden one moves to its section; a disconnected
      // institution shows archived on every row — all before the refetch,
      // which only confirms.
      const hit = new Set(r.affected_accounts);
      if (r.mode === "purge" || r.mode === "disconnect_purge")
        removeFromList<AccountsData, Account>(qc, ["accounts"], "accounts",
          (x) => hit.has(x.id));
      else
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
        // …and the freed slot shows on the allowance NOW. The server
        // archives exactly one item per disconnect (sync/base counts the
        // unarchived ones), so the new figure is known here — the
        // refetch below only confirms it.
        const me = qc.getQueryData<Me>(["me"]);
        const used = me?.features?.institutions_used;
        if (me && typeof used === "number")
          patchQuery<Me>(qc, ["me"],
            { features: { ...me.features, institutions_used:
                            Math.max(0, used - 1) } });
      }
      onDone((r.note || "Removed.") + extra, accountRemoveMoved(r.mode));
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
      {opts.map((o) => (
        <label key={o.mode}
               style={{ display: "block", marginBottom: ".45rem",
                        cursor: "pointer" }}>
          <input type="radio" name={`rm-${a.id}`} value={o.mode}
                 checked={mode === o.mode}
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
                  const need = mode === "disconnect_purge" || mode === "purge";
                  const msg = mode === "disconnect_purge"
                    ? `Disconnect ${inst} and DELETE all its local data? This cannot be undone.`
                    : mode === "purge"
                      ? `Permanently delete ${a.name} and its transactions?`
                      : mode === "disconnect"
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
