// Guided setup for a business entity: a few plain questions -> the entity,
// its accounts, and a startup-cost scan, then the Business page takes over.
//
// A flat form asking for name, structure, state, EIN, formation date and
// start date at once, behind an empty state that says only "No businesses
// yet", asks cold for fields that drive things the form does not show —
// structure picks the Schedule-C buckets, the dates open the §195/§248
// startup-deduction window and the compliance calendar. One question at a
// time, each saying what it decides.
//
// Step 3 (accounts) is deliberately inside the wizard: whole-account
// assignment is what actually separates business money from personal, and
// left on the Accounts page the separation usually never happens. It is
// also the one step that MOVES REAL MONEY between the two sides, so it
// defaults to nothing selected, states plainly what assignment
// does, and is skippable — a wizard invites fast clicking and this is the
// step where fast clicking does damage.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, errText, kindIs } from "../api/client";
import { saveSettings } from "../api/cache";
import { isOwner } from "../role";
import { openPlaidHostedLink } from "./ConnectHub";

const STRUCTURES = [
  ["sole_prop", "Sole proprietorship"],
  ["single_member_llc", "Single-member LLC"],
  ["multi_member_llc", "Multi-member LLC"],
  ["s_corp", "S corporation"],
] as const;

const STEPS = [
  { key: "what", title: "What's the business?",
    blurb: "The structure decides which Schedule-C buckets and filing " +
           "deadlines apply — it's the answer that shapes everything else." },
  { key: "when", title: "Where and when did it start?",
    blurb: "State and dates drive the compliance calendar, and the start " +
           "date opens the startup-cost deduction window (§195/§248)." },
  { key: "accounts", title: "Which accounts hold its money?",
    blurb: "Everything in an assigned account is treated as business money " +
           "and leaves your personal budget and net worth." },
  { key: "startup", title: "Look for startup costs?",
    blurb: "Money spent before the business opened can often be deducted." },
  { key: "done", title: "You're set", blurb: "" },
] as const;

export default function BusinessWizard({ resume, onDone, onCancel }: {
  // Resuming an entity that already exists. The wizard creates the entity at
  // the end of step 2, so anyone who got that far and then closed the tab
  // could not get back to the account step — re-running would have made them
  // a SECOND business. Resume starts at accounts with the entity already in
  // hand, and never calls entityCreate.
  resume?: { id: string; name: string; business_start_date?: string | null };
  onDone: (entityId: string) => void;
  onCancel: () => void;
}) {
  const qc = useQueryClient();
  const [i, setI] = useState(resume ? 2 : 0);
  const [err, setErr] = useState<string | null>(null);
  const [entityId, setEntityId] = useState<string | null>(resume?.id ?? null);

  const [name, setName] = useState(resume?.name ?? "");
  const [structure, setStructure] = useState<string>("single_member_llc");
  const [state, setState] = useState("");
  const [ein, setEin] = useState("");
  const [formation, setFormation] = useState("");
  const [started, setStarted] = useState(resume?.business_start_date ?? "");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [scanned, setScanned] = useState<number | null>(null);
  // a new business almost never has its account connected yet — the first
  // expenses ride a personal card until the business banking exists. Let the
  // step CREATE the account so the wizard is usable on day one.
  const [newName, setNewName] = useState("");
  const [newKind, setNewKind] = useState("checking");
  // A long-lived ledger carries dozens of accounts, and an unfiltered dump
  // is unusable for the one job this step has — "pick the two that belong to
  // this business" — so the long tail of archived / balance-less accounts is
  // hidden until asked for, and there is a search box.
  const [acctQ, setAcctQ] = useState("");
  const [showAll, setShowAll] = useState(false);

  const accounts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  // A member runs this wizard — creating the entity and assigning accounts
  // is editing chrome. The BANK DOOR is not: linking a connection is the
  // account owner's, and the server refuses the rest, so the button that
  // opens it is drawn only for them. Adding the account by hand stays.
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const owner = isOwner(me);
  // Step marks live in tenant config (biz_wizard_steps), exactly as the
  // Welcome wizard does it. Held in React state a refresh forgets
  // everything: no resume, and a "continue setup" prompt that never ends
  // because nothing records completion.
  const settings = useQuery({ queryKey: ["settings"], queryFn: api.settings });
  const marks = (settings.data as { biz_wizard_steps?: Record<string, string> }
                 | undefined)?.biz_wizard_steps || {};
  const mark = useMutation({
    // the save returns the full settings view and seeds the cache with it —
    // the resume point below reads the marks from there
    mutationFn: (m: Record<string, string | null>) =>
      saveSettings(qc, { biz_wizard_steps: m }),
  });
  // The resume point: the first unfinished step, with one correction the
  // Welcome wizard does not need. A mark has to mean "this step's answers
  // reached the server", and the two question steps hold theirs in
  // component state until the entity is created at the end of the SECOND
  // one — so they are marked together, there. A mark left between them by
  // an older build would reopen on "when" with the name blank and no way
  // back to the field that collects it, so an interrupted pair reopens at
  // its first step.
  const open = STEPS.findIndex((s) => s.key !== "done" && !marks[s.key]);
  const firstOpen = open === 1 ? 0 : open;
  // adopt the persisted resume point once settings have loaded, unless the
  // caller pinned a step or the user has already moved within this visit
  const [touched, setTouched] = useState(false);
  useEffect(() => {
    if (touched || resume || settings.isPending) return;
    if (firstOpen > 0) setI(firstOpen);
  }, [firstOpen, touched, resume, settings.isPending]);
  const go = (n: number, key?: string) => {
    setTouched(true);
    if (key) mark.mutate({ [key]: "done" });
    setI(n);
  };


  const step = STEPS[i];
  const fail = (e: unknown) => setErr(errText(e));

  // the entity is created once, at the end of step 2, so steps 3-4 have an id
  const create = useMutation({
    mutationFn: () => api.entityCreate({
      name: name.trim(), structure,
      state: state.trim() || undefined,
      ein: ein.trim() || undefined,
      formation_date: formation || undefined,
      business_start_date: started || undefined,
    } as never),
    onSuccess: (e: { id: string }) => {
      setEntityId(e.id);
      qc.invalidateQueries({ queryKey: ["biz-summary"] });
      // The Business TAB is hidden until a business exists (has_business on
      // /api/me), and this is the moment it starts existing — so refetch `me`
      // here or the tab does not appear until the next full page load, which
      // reads as "the wizard didn't work".
      qc.invalidateQueries({ queryKey: ["me"] });
      setErr(null);
      setTouched(true);
      // both question steps land now: this is the first moment either
      // one's answers exist anywhere but this component
      mark.mutate({ what: "done", when: "done" });
      setI(2);
    },
    onError: fail,
  });

  const assign = useMutation({
    mutationFn: async () => {
      for (const id of picked) await api.accountAssignEntity(id, entityId);
    },
    onSuccess: () => {
      // assignment moves the picked accounts' money to the business side:
      // the accounts, the verdict, the summary and everything derived from
      // the entity's ledger — and the personal ledger views, lenses and
      // reports those transactions just left — not the whole cache
      for (const k of ["accounts", "today", "biz-summary", "pnl", "biz-txns",
                       "balancesheet", "esttax", "biz-suggestions", "vendors",
                       "txns", "lens-month", "lens-year", "report"])
        qc.invalidateQueries({ queryKey: [k] });
      setErr(null);
      go(3, "accounts");
    },
    onError: fail,
  });

  // create + assign in one go: an unassigned manual account would sit in the
  // personal budget, which is the opposite of why it was made
  const addAcct = useMutation({
    mutationFn: async () => {
      const r = await api.accountAdd(newName.trim(), newKind);
      await api.accountAssignEntity(r.account_id, entityId);
      return r;
    },
    onSuccess: () => {
      setNewName("");
      setErr(null);
      // the list is mounted here, so invalidating already refetches it
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
    onError: fail,
  });

  // Connect the real bank. The manual form below makes a placeholder — a
  // container with no feed — and a label like "Business account not
  // connected yet? Create it here" reads as if it would connect one. A real
  // business checking wants live transactions, so the bank link comes first
  // and manual is the explicit fallback.
  const connect = useMutation({
    mutationFn: () => openPlaidHostedLink(),
    onSuccess: (r) => {
      setErr(r?.error ? String(r.error) : null);
      qc.invalidateQueries({ queryKey: ["accounts"] });
      qc.invalidateQueries({ queryKey: ["connections"] });
    },
    onError: fail,
  });

  const scan = useMutation({
    mutationFn: () => api.entityImportFlags(entityId as string),
    onSuccess: (r: unknown) => {
      const n = (r as { imported?: number } | null)?.imported;
      setScanned(typeof n === "number" ? n : 0);
      // the flagged rows became the entity's books: the worksheet, the
      // summary count and the entity's ledger views move; nothing else does
      for (const k of ["biz", "biz-summary", "biz-txns", "pnl",
                       "biz-suggestions"])
        qc.invalidateQueries({ queryKey: [k] });
      setErr(null);
      go(4, "startup");
    },
    onError: fail,
  });

  const busy = create.isPending || assign.isPending || scan.isPending
    || addAcct.isPending || connect.isPending;
  // `kind` is "<type>/<subtype>" on this payload (there is no bare `type`),
  // so it must be matched by prefix — see kindIs. Loans and investments are
  // never business-assignable here.
  const allEligible = (accounts.data?.accounts || [])
    .filter((a) => kindIs(a, "depository", "credit"));
  const live = allEligible.filter(
    (a) => a.status !== "archived" && a.balance_current != null);
  const q = acctQ.trim().toLowerCase();
  const eligible = (showAll ? allEligible : live)
    .filter((a) => !q
      || `${a.name} ${a.institution_name || ""}`.toLowerCase().includes(q))
    .sort((a, b) => `${a.institution_name || ""}${a.name}`
      .localeCompare(`${b.institution_name || ""}${b.name}`));

  return (
    <div className="card" style={{ maxWidth: "40rem" }}>
      <div className="sub" style={{ marginBottom: ".4rem" }}>
        {resume ? <>Continuing setup for <b>{resume.name}</b></>
                : <>Step {i + 1} of {STEPS.length}</>}
      </div>
      {/* passive progress dots, same idea as the Welcome wizard: the flow is
          strict, so these report position rather than offering navigation */}
      <div style={{ display: "flex", gap: ".45rem", alignItems: "center",
                    marginBottom: ".8rem" }}>
        {STEPS.map((s, n) => (
          <span key={s.key} title={`${n + 1}. ${s.title}`}
            style={{ width: 10, height: 10, borderRadius: 5,
                     display: "inline-block",
                     background: n === i ? "var(--amber, #e6b159)"
                       : n < i ? "var(--green, #4a4)" : "var(--line)" }} />
        ))}
      </div>
      <h2 style={{ marginTop: 0 }}>{step.title}</h2>
      {step.blurb && <p className="mut">{step.blurb}</p>}
      {err && <div className="note bad">{err}</div>}

      {step.key === "what" && (
        <div className="field-grid">
          <label>Name
            <input value={name} autoFocus placeholder="Acme LLC"
                   onChange={(e) => setName(e.target.value)} />
          </label>
          <label>Structure
            <select value={structure}
                    onChange={(e) => setStructure(e.target.value)}>
              {STRUCTURES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select>
          </label>
        </div>
      )}

      {step.key === "when" && (
        <div className="field-grid">
          <label>State <span className="mut">(optional)</span>
            <input value={state} placeholder="CA"
                   onChange={(e) => setState(e.target.value)} />
          </label>
          <label>EIN <span className="mut">(optional, stored encrypted)</span>
            <input value={ein} placeholder="12-3456789"
                   onChange={(e) => setEin(e.target.value)} />
          </label>
          <label>Formation date <span className="mut">(optional)</span>
            <input type="date" value={formation}
                   onChange={(e) => setFormation(e.target.value)} />
          </label>
          <label>Business start date <span className="mut">(optional)</span>
            <input type="date" value={started}
                   onChange={(e) => setStarted(e.target.value)} />
          </label>
        </div>
      )}

      {step.key === "accounts" && (
        <>
          <div className="note" style={{ marginBottom: ".6rem" }}>
            Assigning an account moves <b>all</b> of its transactions —
            past and future — out of your personal budget, cash flow and net
            worth. Only pick accounts the business actually owns. You can
            change this any time on the Accounts page.
          </div>
          {accounts.isPending && <p className="mut">loading accounts…</p>}
          <div style={{ display: "flex", gap: ".5rem", alignItems: "center",
                        flexWrap: "wrap", marginBottom: ".5rem" }}>
            <input value={acctQ} placeholder="search accounts"
                   onChange={(e) => setAcctQ(e.target.value)}
                   style={{ flex: "1 1 12rem" }} />
            <button onClick={() => { accounts.refetch(); }}>Refresh</button>
            {allEligible.length !== live.length && (
              <button className="linklike mut"
                      style={{ background: "none", border: "none",
                               cursor: "pointer", color: "var(--blue)",
                               textDecoration: "underline" }}
                      onClick={() => setShowAll(!showAll)}>
                {showAll ? `hide ${allEligible.length - live.length} closed/archived`
                         : `show ${allEligible.length - live.length} closed/archived`}
              </button>
            )}
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: ".3rem" }}>
            {eligible.map((a) => (
              <label key={a.id} style={{ display: "flex", gap: ".5rem",
                                         alignItems: "center" }}>
                <input type="checkbox" checked={picked.has(a.id)}
                  onChange={(e) => {
                    const next = new Set(picked);
                    if (e.target.checked) next.add(a.id); else next.delete(a.id);
                    setPicked(next);
                  }} />
                <span>{a.name}</span>
                <span className="mut">{a.kind}</span>
              </label>
            ))}
          </div>
          <div style={{ marginTop: ".9rem", borderTop: "1px solid var(--line, #333)",
                        paddingTop: ".7rem" }}>
            <div className="sub" style={{ marginBottom: ".5rem" }}>
              Business bank not connected yet?
            </div>
            {owner ? (
              <>
                <button className="pri" disabled={busy}
                        onClick={() => connect.mutate()}>
                  {connect.isPending ? "waiting for your bank…"
                                     : "Connect an account"}</button>
                <div className="sub" style={{ marginTop: ".35rem" }}>
                  Opens your bank in a new tab. When it finishes, the account
                  appears in the list above — tick it and assign.
                  {connect.isPending && <> Closed the bank tab already?{" "}
                    <button className="linklike"
                            style={{ background: "none", border: "none",
                                     cursor: "pointer", color: "var(--blue)",
                                     textDecoration: "underline", padding: 0 }}
                            onClick={() => { connect.reset();
                                             accounts.refetch(); }}>
                      refresh the list
                    </button>.</>}
                </div>
              </>
            ) : (
              <div className="sub">Linking a bank over this household is the
                account owner's — ask them, and the business account shows up
                in the list above for you to assign. You can still track it by
                hand below.</div>
            )}
            <div className="sub" style={{ marginTop: ".9rem",
                                          marginBottom: ".4rem" }}>
              Or track it by hand. A manual account holds a balance you
              maintain yourself — <b>no transactions will flow into it</b>.
              Use this only when the bank cannot be connected.
            </div>
            <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                          alignItems: "flex-end" }}>
              <label>Account name
                <input value={newName} placeholder="Acme LLC Checking"
                       onChange={(e) => setNewName(e.target.value)} />
              </label>
              <label>Type
                <select value={newKind}
                        onChange={(e) => setNewKind(e.target.value)}>
                  <option value="checking">Checking</option>
                  <option value="savings">Savings</option>
                  <option value="credit">Credit card</option>
                </select>
              </label>
              <button disabled={busy || !newName.trim()}
                      onClick={() => addAcct.mutate()}>
                {addAcct.isPending ? "adding…" : "Add account"}</button>
            </div>
          </div>
        </>
      )}

      {step.key === "when" && !name.trim() && (
        <div className="note">This business still needs a name — go back to
          the first question and give it one.</div>
      )}

      {step.key === "startup" && (
        <p className="mut">
          {started
            ? <>Scan transactions before <b>{started}</b> for costs that may
                qualify as startup expenses. Nothing is deducted
                automatically — matches are flagged for you to review.</>
            : <>You didn't set a business start date, so there's no window to
                scan. You can add one later and run this from the Business
                page.</>}
        </p>
      )}

      {step.key === "done" && (
        <p className="mut">
          <b>{name || "Your business"}</b> is set up
          {picked.size ? ` with ${picked.size} account${picked.size > 1 ? "s" : ""}` : ""}
          {scanned ? `, and ${scanned} possible startup cost${scanned > 1 ? "s" : ""} flagged` : ""}.
          Everything is editable on the Business page.
        </p>
      )}

      <div style={{ display: "flex", gap: ".5rem", marginTop: "1rem",
                    flexWrap: "wrap", alignItems: "center" }}>
        {step.key === "what" && (
          <button className="pri" disabled={!name.trim()}
                  onClick={() => { setErr(null); go(1); }}>Continue</button>
        )}
        {step.key === "when" && (
          <>
            {/* the name is collected a step earlier and is required by the
                server, so a blank one must not be offerable here */}
            <button className="pri" disabled={busy || !name.trim()}
                    onClick={() => create.mutate()}>
              {create.isPending ? "creating…" : "Create business"}</button>
            {/* the only step with a way back. Everything after this one has
                already created the entity, so re-entering the questions
                would offer to create a second business. */}
            <button disabled={busy}
                    onClick={() => { setErr(null); setTouched(true);
                                     setI(0); }}>Back</button>
          </>
        )}
        {step.key === "accounts" && (
          <>
            <button className="pri" disabled={busy || picked.size === 0}
                    onClick={() => assign.mutate()}>
              {assign.isPending ? "assigning…"
                : `Assign ${picked.size || ""} account${picked.size === 1 ? "" : "s"}`}</button>
            <button disabled={busy} onClick={() => {
              setErr(null); setTouched(true);
              mark.mutate({ accounts: "skipped" }); setI(3); }}>
              Skip for now</button>
          </>
        )}
        {step.key === "startup" && (
          <>
            {started && (
              <button className="pri" disabled={busy}
                      onClick={() => scan.mutate()}>
                {scan.isPending ? "scanning…" : "Scan for startup costs"}</button>
            )}
            <button disabled={busy} onClick={() => {
              setErr(null); setTouched(true);
              mark.mutate({ startup: started ? "skipped" : "done" });
              setI(4); }}>
              {started ? "Skip" : "Continue"}</button>
          </>
        )}
        {step.key === "done" && (
          <button className="pri"
                  onClick={() => { mark.mutate({ finish: "done" });
                                   onDone(entityId as string); }}>
            Open the Business page</button>
        )}
        {step.key !== "done" && (
          <button className="linklike mut" disabled={busy}
                  style={{ background: "none", border: "none",
                           cursor: "pointer", marginLeft: "auto" }}
                  onClick={() => (entityId ? onDone(entityId) : onCancel())}>
            {entityId ? "Finish later" : "Cancel"}</button>
        )}
      </div>
    </div>
  );
}
