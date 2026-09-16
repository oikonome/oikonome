// Settings → Connections → Manage accounts: an accordion whose
// rows are quiet and scannable — name · mask · type
// · state pills · balance — and exactly ONE editor opens at a time.
// Connection actions live on the institution header: ↻ sync
// always, fix ↗ PROMOTED out of the menu when the connection is broken
// (that is the moment it is the most important thing on the page), the
// rest behind ⋯. The Accounts page stays the balances/holdings view;
// both surfaces drive the same API doors.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, errText, money, type Account, type Connection,
         type Settings } from "../api/client";
import { patchList, removeFromList } from "../api/cache";
import { openPlaidHostedLink } from "./ConnectHub";
import { KINDS, RemoveAccountPanel, type DoneFn,
         type Moved } from "./RemoveAccountPanel";
import { isOwner } from "../role";

interface Grp {
  itemId: string;
  institution: string;
  conn: Connection | null;
  accounts: Account[];
}

export default function ManageAccounts() {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  // Everything on this panel acts on a CONNECTION — relink, choose what
  // the bank shares, disconnect, hide, remove — so it is the owner's
  // whole. A member renames and classifies accounts from the table on the
  // Accounts page, which is the part of this that is data.
  const owner = isOwner(me);
  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const conns = useQuery({ queryKey: ["connections"],
                           queryFn: api.connections });
  const [notice, setNotice] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [syncing, setSyncing] = useState<string | null>(null);

  // Callers say what their write moved; the editors below patch the cached
  // rows first, so the refetch confirms rather than reveals. No list means
  // everything — the Plaid relink paths really do change all of it.
  const done: DoneFn = (msg, moved) => {
    setNotice(msg);
    const keys: Moved[] = moved
      ?? ["accounts", "connections", "settings", "today", "me"];
    for (const k of keys) qc.invalidateQueries({ queryKey: [k] });
    // Belt and braces for a caller that named "connections" without "me":
    // the institutions-used figure rides /api/me, and every path that
    // moves a connection moves it too.
    if (keys.includes("connections") && !keys.includes("me"))
      qc.invalidateQueries({ queryKey: ["me"] });
  };

  const sync = useMutation({
    mutationFn: (itemId: string) => api.accountSync(itemId),
    onMutate: (itemId) => setSyncing(itemId),
    onSuccess: () => done("Synced.", ["accounts", "connections", "today"]),
    onError: (e) => done(errText(e)),
    onSettled: () => setSyncing(null),
  });

  if (!owner)
    return <p className="mut">Account management is owner-only.</p>;
  if (!accounts.data || !conns.data)
    return <p className="mut">loading…</p>;

  const primary = settings.data?.checking_account_id ?? null;
  const excluded = new Set(settings.data?.excluded_accounts ?? []);
  const connById = new Map(conns.data.connections.map((c) => [c.id, c]));
  const groups = new Map<string, Grp>();
  for (const a of accounts.data.accounts) {
    const key = a.item_id || "manual";
    let g = groups.get(key);
    if (!g) {
      g = { itemId: a.item_id, institution: a.institution_name || "Manual",
            conn: connById.get(a.item_id) ?? null, accounts: [] };
      groups.set(key, g);
    }
    g.accounts.push(a);
  }
  const ordered = [...groups.values()].sort((x, y) =>
    Number(y.conn !== null) - Number(x.conn !== null)
    || x.institution.localeCompare(y.institution));

  return (
    <div>
      {notice && (
        <p className="note" role="status" onClick={() => setNotice(null)}>
          {notice}</p>
      )}
      {ordered.map((g) => (
        <GroupPanel key={g.itemId || g.institution} g={g} primary={primary}
                    excluded={excluded} openId={openId} setOpenId={setOpenId}
                    syncing={syncing === g.itemId}
                    onSync={() => sync.mutate(g.itemId)} onDone={done} />
      ))}
      <p className="sub" style={{ marginTop: ".8rem" }}>
        Balances, holdings and history live on{" "}
        <Link to="/accounts">Accounts</Link> · add a bank or import files
        from the providers above.</p>
    </div>
  );
}

function GroupPanel({ g, primary, excluded, openId, setOpenId, syncing,
                      onSync, onDone }: {
  g: Grp; primary: string | null; excluded: Set<string>;
  openId: string | null; setOpenId: (id: string | null) => void;
  syncing: boolean; onSync: () => void; onDone: DoneFn;
}) {
  const qc = useQueryClient();
  const c = g.conn;
  const live = c !== null && c.status !== "archived" && c.status !== "reaped";
  const broken = live && c!.status !== "ok" && c!.status !== null;
  const plaid = c?.aggregator === "plaid";
  const disc = useMutation({
    mutationFn: () => api.connectionDisconnect(g.itemId),
    onSuccess: (r) => {
      // a disconnected connection is not served by the list any more, and
      // the header pill reads that list from the cache — drop it now rather
      // than after the next poll lands
      removeFromList<{ connections: Connection[] }, Connection>(
        qc, ["connections"], "connections", (x) => x.id === g.itemId);
      // "me" — the freed institution slot is on the plan allowance
      onDone(r.note || "Disconnected — history kept.",
             ["accounts", "connections", "today", "me"]);
    },
    onError: (e) => onDone(errText(e)),
  });
  // one Link session per click — the bank tab is slow to come back
  const [linking, setLinking] = useState(false);
  const closeMenu = (e: React.MouseEvent) =>
    (e.currentTarget as HTMLElement).closest("details")
      ?.removeAttribute("open");
  return (
    <div className="macct-grp">
      <div className="macct-head">
        <b>{g.institution}</b>
        {c === null
          ? <span className="pill m">manual / import</span>
          : broken
            ? <span className="pill r">{c!.status === "login_required"
                ? "sign-in expired" : "needs attention"}</span>
            : live ? <span className="pill g">connected</span>
                   : <span className="pill m">{c!.status}</span>}
        <span className="macct-headact">
          {live && (
            <button type="button" className="macct-quiet" disabled={syncing}
                    onClick={onSync} title="pull from this institution now">
              {syncing ? "syncing…" : "↻ sync"}</button>
          )}
          {broken && plaid && (
            <button type="button" className="pri" disabled={linking}
                    title="re-authenticate this connection"
                    onClick={() => {
                      setLinking(true);
                      void openPlaidHostedLink(g.itemId)
                      .then((s) => onDone(
                        s.kind === "update_failed"
                          ? "Reconnect didn't complete — the connection "
                            + "still reports an error"
                            + (s.error ? ` (${s.error})` : "") + "."
                          : "Reconnected."))
                      .catch((e) => onDone(errText(e)))
                      .finally(() => setLinking(false));
                    }}>
              fix ↗</button>
          )}
          {live && (plaid || c !== null) && (
            <details className="usermenu macct-menu">
              <summary title="more connection actions">⋯</summary>
              <div className="usermenu-panel">
                {plaid && (
                  <button type="button" disabled={linking} onClick={(e) => {
                      closeMenu(e);
                      setLinking(true);
                      void openPlaidHostedLink(g.itemId,
                          { manageAccounts: true })
                        .then((s) => onDone(
                          s.kind === "update_failed"
                            ? "That didn't complete — the connection "
                              + "still reports an error"
                              + (s.error ? ` (${s.error})` : "") + "."
                            : s.kind === "exited"
                              ? "No change — the window was closed."
                              : "Connected accounts updated."))
                        .catch((err) => onDone(errText(err)))
                        .finally(() => setLinking(false));
                    }}
                    title={"choose what " + g.institution + " shares — "
                           + "unshared accounts stop syncing and stop "
                           + "counting toward Plaid usage"}>
                    edit connected accounts ↗</button>
                )}
                <button type="button" style={{ color: "var(--red, #c44)" }}
                        disabled={disc.isPending}
                        onClick={(e) => {
                          closeMenu(e);
                          if (window.confirm(
                              `Disconnect ${g.institution}? It stops `
                              + "syncing; history is kept."))
                            disc.mutate();
                        }}>
                  disconnect…</button>
              </div>
            </details>
          )}
        </span>
      </div>
      {g.accounts.map((a) => (
        <AccountRow key={a.id} a={a} isPrimary={a.id === primary}
                    isExcluded={excluded.has(a.id)} live={live}
                    open={openId === a.id}
                    onToggle={() => setOpenId(openId === a.id ? null : a.id)}
                    onDone={onDone} />
      ))}
    </div>
  );
}

function AccountRow({ a, isPrimary, isExcluded, live, open, onToggle,
                      onDone }: {
  a: Account; isPrimary: boolean; isExcluded: boolean; live: boolean;
  open: boolean; onToggle: () => void; onDone: DoneFn;
}) {
  const hidden = !!a.user_removed_at;
  const kindLabel = KINDS.find(([v]) => v === a.kind)?.[1]
    ?? a.kind.split("/")[0];
  return (
    <>
      <button type="button"
              className={"macct-row" + (hidden ? " dim" : "")}
              aria-expanded={open} onClick={onToggle}>
        <span className="macct-caret">{open ? "▾" : "▸"}</span>
        <span className="macct-nm">{a.name}</span>
        {a.mask && <span className="mut macct-sub">·{a.mask}</span>}
        <span className="mut macct-sub macct-kind">{kindLabel}</span>
        {isPrimary && <span className="pill b">primary</span>}
        {isExcluded && <span className="pill m">excluded</span>}
        {hidden && <span className="pill m">hidden</span>}
        <span className="macct-bal">
          {a.balance_current !== null ? money(a.balance_current) : ""}</span>
      </button>
      {open && <Editor a={a} isPrimary={isPrimary} isExcluded={isExcluded}
                       live={live} hidden={hidden} onDone={onDone} />}
    </>
  );
}

function Editor({ a, isPrimary, isExcluded, live, hidden, onDone }: {
  a: Account; isPrimary: boolean; isExcluded: boolean; live: boolean;
  hidden: boolean; onDone: DoneFn;
}) {
  const qc = useQueryClient();
  const [name, setName] = useState(a.name);
  const [kind, setKind] = useState(a.kind);
  const [ownerLbl, setOwnerLbl] = useState(a.owner ?? "");
  const [prim, setPrim] = useState(isPrimary);
  const [excl, setExcl] = useState(isExcluded);
  const [bal, setBal] = useState(
    a.balance_current === null ? "" : String(a.balance_current));
  const [removing, setRemoving] = useState(false);
  const manual = a.institution_name === "Manual" || a.aggregator === "manual";

  const dirty = name !== a.name || kind !== a.kind
    || ownerLbl !== (a.owner ?? "") || prim !== isPrimary
    || excl !== isExcluded
    || (manual && bal.trim() !== "" &&
        Number(bal.replace(/[$,]/g, "")) !== a.balance_current);

  const save = useMutation({
    mutationFn: async () => {
      // Each field is its own API door, so a mid-sequence failure leaves
      // the earlier ones already persisted. The error has to say which
      // changes landed and which didn't, or the person retries from a
      // state they believe is untouched.
      const steps: [string, () => Promise<unknown>][] = [];
      if (name.trim() && name !== a.name)
        steps.push(["name", () => api.accountRename(a.id, name)]);
      if (kind !== a.kind || prim !== isPrimary)
        steps.push([
          kind !== a.kind && prim !== isPrimary ? "type/primary"
            : kind !== a.kind ? "type" : "primary flag",
          () => api.accountClassify(a.id, kind, prim)]);
      if (excl !== isExcluded)
        steps.push(["exclusion", () => api.accountExclude(a.id, excl)]);
      if (ownerLbl.trim() !== (a.owner ?? ""))
        steps.push(["owner", () => api.setAccountOwner(a.id,
                                                       ownerLbl.trim())]);
      if (manual && bal.trim() !== "" &&
          Number(bal.replace(/[$,]/g, "")) !== a.balance_current)
        steps.push(["balance", () => api.accountBalance(a.id, bal)]);
      const saved: string[] = [];
      for (const [label, run] of steps) {
        try { await run(); }
        catch (e) {
          const why = errText(e);
          const rest = steps.slice(saved.length + 1).map(([l]) => l);
          throw new Error(
            (saved.length ? `Saved ${saved.join(", ")}, but ` : "")
            + `${label} failed: ${why}`
            + (rest.length ? ` (${rest.join(", ")} not attempted)` : ""));
        }
        saved.push(label);
      }
    },
    onSuccess: () => {
      // `dirty` and the row header both read the cache, so seed it with
      // what was just written: Save greys out and the header shows the new
      // name in the same frame, instead of after two refetches. Every door
      // above is a plain field write, so the prediction is exact.
      const newBal = Number(bal.replace(/[$,]/g, ""));
      patchList<{ accounts: Account[] }, Account>(qc, ["accounts"], "accounts",
        (x) => x.id === a.id,
        { name: name.trim() || a.name, kind, owner: ownerLbl.trim() || null,
          ...(manual && bal.trim() !== "" && Number.isFinite(newBal)
              ? { balance_current: newBal } : {}) });
      const moved = prim !== isPrimary || excl !== isExcluded;
      if (moved)
        qc.setQueryData<Settings>(["settings"], (old) => old && {
          ...old,
          checking_account_id: prim ? a.id
            : old.checking_account_id === a.id ? null
            : old.checking_account_id,
          excluded_accounts: excl
            ? [...new Set([...(old.excluded_accounts ?? []), a.id])]
            : (old.excluded_accounts ?? []).filter((id) => id !== a.id),
        });
      // the anchor and the exclusion move the verdict; a rename does not
      onDone(`Saved ${name || a.name}.`,
             moved ? ["accounts", "today"] : ["accounts"]);
    },
    onError: (e) => onDone(errText(e)),
  });
  const setHidden = useMutation({
    mutationFn: (h: boolean) => api.accountSetHidden(a.id, h),
    onSuccess: (r, h) => {
      // the row's hidden pill and dim state read the cache; a hidden
      // account also drops its balance, which is what keeps it out of
      // every total
      patchList<{ accounts: Account[] }, Account>(qc, ["accounts"], "accounts",
        (x) => x.id === a.id,
        h ? { user_removed_at: new Date().toISOString(), balance_current: null }
          : { user_removed_at: null });
      onDone(r.note, ["accounts", "today"]);
    },
    onError: (e) => onDone(errText(e)),
  });

  const field = (label: string, node: React.ReactNode) => (
    <span>
      <span className="macct-lbl">{label}</span>
      {node}
    </span>
  );

  return (
    <div className="macct-ed">
      <form className="macct-fields"
            onSubmit={(e) => { e.preventDefault(); save.mutate(); }}>
        {field("Name",
          <input type="text" value={name} style={{ width: "12rem" }}
                 title="display name — sync never overwrites it"
                 onChange={(e) => setName(e.target.value)} />)}
        {field("Type",
          <select value={kind} onChange={(e) => setKind(e.target.value)}>
            {KINDS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            {!KINDS.some(([v]) => v === a.kind) &&
              <option value={a.kind}>{a.kind}</option>}
          </select>)}
        {field("Owner",
          <input type="text" value={ownerLbl} placeholder="ours"
                 style={{ width: "6rem" }}
                 title="whose money — a reporting filter, never the budget"
                 onChange={(e) => setOwnerLbl(e.target.value)} />)}
        {manual && field("Balance",
          <input value={bal} size={8} placeholder="0.00"
                 style={{ textAlign: "right" }}
                 onChange={(e) => setBal(e.target.value)} />)}
        <label className="mut macct-chk"
               title="anchor the cash forecast on this account">
          <input type="checkbox" checked={prim}
                 onChange={(e) => setPrim(e.target.checked)} /> primary
        </label>
        <label className="mut macct-chk"
               title="exclude from budget math — its spending never counts">
          <input type="checkbox" checked={excl}
                 onChange={(e) => setExcl(e.target.checked)} /> excluded
        </label>
      </form>
      <div className="macct-acts">
        <button type="button" className="pri"
                disabled={!dirty || save.isPending}
                onClick={() => save.mutate()}>
          {save.isPending ? "saving…" : "Save"}</button>
        <button type="button" disabled={setHidden.isPending}
                title={hidden
                  ? "bring this account back into totals and lists"
                  : "hide from every total, list and sync — connection and "
                    + "history stay"}
                onClick={() => {
                  const to = !hidden;
                  if (!to || window.confirm(
                      `Hide ${a.name}? It stops pulling new data and will `
                      + `not appear in or count toward anything.`))
                    setHidden.mutate(to);
                }}>
          {hidden ? "Unhide" : "Hide"}</button>
        <span style={{ flex: 1 }} />
        <button type="button" style={{ color: "var(--red, #c44)" }}
                onClick={() => setRemoving((v) => !v)}>
          {removing ? "close" : "Remove…"}</button>
      </div>
      {removing && (
        <RemoveAccountPanel a={a} liveConn={live}
          onDone={onDone} onCancel={() => setRemoving(false)} />
      )}
    </div>
  );
}
