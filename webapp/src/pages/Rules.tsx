// the learned-rules surface. Every categorization rule the system
// applies — with provenance (your correction / model / seed) — visible and
// controllable: change the category from the same chip the ledger uses, or
// turn a rule off / delete it from its ⋯ strip. No hand-authoring: rules are
// learned; this page just shows and corrects them. Owner-only edits; viewers
// see the list read-only.
//
// One section per provenance — "Your
// rules" expanded, From an AI pass / Your trained model / Built-in collapsed to a count. Groups
// load LAZILY with server-side search + pagination (~50/page; a real
// ledger has thousands of rules and nothing may render them all at once).
// Editing or disabling a model/seed rule PROMOTES it to source='user' and
// it moves to "Your rules" — the Model group stays purely untouched
// inferences, and it hides entirely at zero (an instance with no LLM
// never shows it). No group-level kill switch; per-rule disable only.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, errText, toast, type RuleRow, type RulesPage, catLabel } from "../api/client";
import { invalidateRollups, patchList, removeFromList } from "../api/cache";
import { canEdit } from "../role";
import DemoLock from "../components/DemoLock";
import { spendPickerGroups, UNKNOWN_FIGURE } from "../moneymath";
import PageSkeleton from "../components/PageSkeleton";
import { FailedLine } from "../components/Loaded";
import Disclosure from "../components/Disclosure";
import Note from "../components/Note";

const SOURCE_LABEL: Record<string, string> = {
  user: "your correction",
  llm: "classified by an AI pass",
  seed: "matched seed rule",
  model: "your trained model, from your corrections",
};
type Source = "user" | "llm" | "seed" | "model";
type Cats = Awaited<ReturnType<typeof api.categories>>;

export default function Rules({ embedded = false }:
                                   { embedded?: boolean } = {}) {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  // the demo instance refuses every rule write and the rename —
  // disabled-in-place, as Merchants does, beats a refusal after acting
  const demo = !!me.data?.demo;
  const [filter, setFilter] = useState("");
  const [q, setQ] = useState("");   // debounced — server-side search
  useEffect(() => {
    const t = window.setTimeout(() => setQ(filter.trim()), 300);
    return () => window.clearTimeout(t);
  }, [filter]);

  // "Your rules" is the always-loaded group; its response also carries the
  // q-filtered per-source counts the collapsed headers need — no rows load
  // for a group until it's expanded.
  // A new search keeps the page, search box and all (focus included), with
  // the previous answer dimmed until the new one lands.
  const userQ = useQuery({ queryKey: ["rules", "user", q, 1],
                           queryFn: () => api.rules("user", q, 1),
                           placeholderData: (prev) => prev });
  const counts = userQ.data?.counts;
  // all FOUR provenances count — the trained classifier ("model") is a
  // source like any other, and leaving it out would both undercount the
  // header and let a model-only ledger render as "no rules yet".
  const total = counts
    ? counts.user + counts.llm + counts.seed + counts.model : 0;
  const empty = !q && !!counts && total === 0;

  if (userQ.isPending)
    return <PageSkeleton />;
  // a failed read keeps the hero and its search box; the count is unknown
  // and the failed line stands where the groups would
  const failed = userQ.data === undefined;

  return (
    <>
      {!embedded && <h1>Categorization rules</h1>}
      {/* The hero states the fact rather than opening with explanatory
          paragraphs before a single rule appears, and the search box lives
          in it because it filters all three groups below. */}
      <div className={embedded ? undefined : "card"} style={{ marginTop: 0 }}>
        <div style={{ display: "flex", gap: ".9rem", alignItems: "baseline",
                      flexWrap: "wrap" }}>
          <span style={{ fontSize: "1.35rem", fontWeight: 700 }}>
            {failed ? `${UNKNOWN_FIGURE} rules`
              : `${total.toLocaleString()} rule${total === 1 ? "" : "s"}`}</span>
          {/* the server's words for where each rule came from: "model"
              alone named two different things (an AI pass and the
              household's own trained classifier) */}
          <span className="sub">sorting your transactions
            {userQ.data && <> · {userQ.data.counts_text}</>}</span>
          {!empty && (
            <input aria-label="Search merchants or categories"
                   value={filter} onChange={(e) => setFilter(e.target.value)}
                   placeholder="search merchants or categories"
                   style={{ marginLeft: "auto", flex: "1 1 17rem",
                            maxWidth: "22rem", minWidth: 0 }} />)}
        </div>
        <div className="sub" style={{ marginTop: ".2rem" }}>Rules are learned,
          never hand-written. Correcting or disabling one makes it yours.</div>
        {mayEdit && demo && (
          <div className="sub" style={{ marginTop: ".4rem", color: "var(--amber)" }}>
            Not available on the demo instance — synthetic data only.
          </div>
        )}
      </div>

      {failed ? (
        <FailedLine q={userQ} what={q ? `rules matching “${q}”` : "rules"} />
      ) : empty ? (
        <div className={embedded ? undefined : "card stickyhead"}>
          <p className="mut" style={{ margin: 0 }}>
            No rules yet — they appear as Oikonome learns your merchants:
            built-in rules match on your first import, the model classifies
            the rest when an LLM is configured (Settings), and a correction you
            apply to every transaction from a merchant becomes a rule of your
            own (“Just this one” changes only that row).
          </p>
        </div>
      ) : (<div style={userQ.isPlaceholderData ? { opacity: 0.55 } : undefined}>
        <div className={embedded ? undefined : "card stickyhead"}>
          <RuleGroup source="user" title="Your rules" mayEdit={mayEdit} demo={demo} q={q}
                     note="corrections you made"
                     count={counts?.user ?? 0} defaultOpen />
        </div>
        <div className={embedded ? undefined : "card stickyhead"}>
          {(counts?.llm ?? 0) > 0 && (
            <RuleGroup source="llm" title="From an AI pass" mayEdit={mayEdit} demo={demo} q={q}
                       note="classified by an AI endpoint; correcting one makes it yours"
                       count={counts?.llm ?? 0} />)}
          {(counts?.model ?? 0) > 0 && (
            <RuleGroup source="model" title="Your trained model" mayEdit={mayEdit} demo={demo} q={q}
                       note="the classifier trained on your household's own corrections; correcting one makes it yours"
                       count={counts?.model ?? 0} />)}
          {(counts?.seed ?? 0) > 0 && (
            <RuleGroup source="seed" title="Built-in" mayEdit={mayEdit} demo={demo} q={q}
                       note="shipped starting points"
                       count={counts?.seed ?? 0} />)}
        </div>
      </div>)}

      {/* the page's rarest action, below its content */}
      {mayEdit && (
        <Disclosure card={!embedded}
          style={embedded ? { marginTop: "1rem" } : undefined}
          summary={<span className="mut" style={{ fontSize: 13.5 }}>
            Rename a custom category…</span>}>
          <DemoLock on={demo}><RenameCategoryCard /></DemoLock>
        </Disclosure>
      )}
    </>
  );
}

/** Free-form custom categories (✎ custom category…) are just strings — this
 *  rewrites the name on every override, merchant rule, and budget carve-out
 *  that used the old string. */
function RenameCategoryCard() {
  const qc = useQueryClient();
  const cats = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const rename = useMutation({
    mutationFn: () => api.categoryRename(from.trim(), to.trim()),
    onSuccess: (r) => {
      const n = r.overrides + r.primaries + r.rules + r.buckets;
      setMsg(`Renamed “${r.old}” → “${r.new}” `
        + `(${r.overrides} overrides, ${r.primaries} rows, `
        + `${r.rules} rules, ${r.buckets} budget matches).`);
      setErr(null);
      setFrom(""); setTo("");
      qc.invalidateQueries({ queryKey: ["categories"] });
      qc.invalidateQueries({ queryKey: ["rules"] });
      qc.invalidateQueries({ queryKey: ["txns"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
      // the carve-outs it rewrote feed the verdict and the month snapshots
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["budget-snapshots"] });
      invalidateRollups(qc);
      if (n === 0) setMsg(`Nothing used “${r.old}” — name unchanged elsewhere.`);
    },
    onError: (e) => {
      setMsg(null);
      setErr(errText(e));
    },
  });
  // Only free-form / custom names — never the standard Plaid primaries.
  const standard = new Set([
    ...(cats.data?.plaid_spend ?? []),
    ...(cats.data?.plaid_flow ?? []),
  ]);
  const options = (cats.data?.categories ?? [])
    .filter((c) => c && !standard.has(c));
  return (
    <div style={{ margin: "0 0 1rem", padding: ".75rem",
                  border: "1px solid var(--line)", borderRadius: "var(--rs)",
                  background: "var(--hover)" }}>
      <div style={{ fontWeight: 600, marginBottom: ".35rem" }}>
        Rename a category
      </div>
      <p className="mut" style={{ fontSize: 13, marginTop: 0 }}>
        Only <b>custom names you created</b> (✎ custom category…). Standard
        bank/Plaid labels stay fixed. Rename rewrites every transaction
        override, merchant rule, and budget carve-out that used the old name.
      </p>
      <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap",
                    alignItems: "center" }}>
        <select aria-label="Category to rename"
                value={from} onChange={(e) => setFrom(e.target.value)}
                style={{ minWidth: "12rem" }}>
          <option value="">
            {/* "none yet" only from a list that answered */}
            {options.length ? "your custom name…"
              : cats.data === undefined ? "Loading…"
              : "no custom categories yet"}
          </option>
          {options.map((c) => (
            <option key={c} value={c}>{catLabel(c)}</option>
          ))}
        </select>
        <span className="mut">→</span>
        <input aria-label="New name" value={to} placeholder="new name"
               onChange={(e) => setTo(e.target.value)}
               style={{ minWidth: "12rem" }} />
        <button className="pri"
                disabled={rename.isPending || !from.trim() || !to.trim()
                  || from.trim() === to.trim()}
                onClick={() => rename.mutate()}>
          {rename.isPending ? "renaming…" : "Rename"}
        </button>
      </div>
      {msg && <Note tone="good" style={{ marginTop: ".5rem" }}>{msg}</Note>}
      {err && <Note tone="bad" style={{ marginTop: ".5rem" }}
                   onDismiss={() => setErr(null)}>{err}</Note>}
    </div>
  );
}

function RuleGroup({ source, title, mayEdit, demo, q, count, note,
                    defaultOpen = false }: {
  source: Source; title: string; mayEdit: boolean; demo: boolean; q: string;
  note: string;
  count: number; defaultOpen?: boolean;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(defaultOpen);
  const [page, setPage] = useState(1);
  useEffect(() => setPage(1), [q]);     // a new search restarts at page 1
  const key = ["rules", source, q, page];
  // a new search or page keeps the rows on screen until it answers
  const rq = useQuery({ queryKey: key,
                        queryFn: () => api.rules(source, q, page),
                        enabled: open,
                        placeholderData: (prev) => prev });
  // one taxonomy fetch for the group, not one observer per row
  const cats = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  // A write touches one rule: this page's cached rows are patched first, so
  // the row changes (or leaves) in the same frame the response lands. Then
  // only the groups the write really moved are refetched — this one (every
  // page and search of it: a row leaving shifts the rows behind it), and
  // "Your rules", which carries every group's counts and is where an edited
  // or disabled model/seed rule is promoted to. Only mounted pages fetch.
  const refetch = () => {
    qc.invalidateQueries({ queryKey: ["rules", "user"] });
    if (source !== "user") qc.invalidateQueries({ queryKey: ["rules", source] });
  };
  const disable = useMutation({
    mutationFn: ({ m, d }: { m: string; d: boolean }) => api.ruleDisable(m, d),
    onSuccess: (_r, v) => {
      if (source === "user")
        patchList<RulesPage, RuleRow>(qc, key, "rules",
          (r) => r.merchant === v.m, { disabled: v.d });
      else   // promoted to "Your rules"
        removeFromList<RulesPage, RuleRow>(qc, key, "rules",
          (r) => r.merchant === v.m);
      refetch();
      // turning a rule back on re-applies it to past transactions, so the
      // ledger and the verdict move like they do after a category edit
      if (!v.d) {
        qc.invalidateQueries({ queryKey: ["txns"] });
        qc.invalidateQueries({ queryKey: ["today"] });
        qc.invalidateQueries({ queryKey: ["merchant-detail"] });
        invalidateRollups(qc);
      }
    },
    // a refused write only re-enabled the button, which reads as a no-op
    onError: (e, v) => toast(
      `Couldn't turn ${v.d ? "off" : "on"} the rule for ${v.m}: ${errText(e)}`),
  });
  const del = useMutation({
    mutationFn: (m: string) => api.ruleDelete(m),
    onSuccess: (_r, m) => {
      removeFromList<RulesPage, RuleRow>(qc, key, "rules",
        (r) => r.merchant === m);
      refetch();
    },
    onError: (e, m) => toast(`Couldn't delete the rule for ${m}: ${errText(e)}`),
  });
  // A delete cannot be undone, so it asks first — like every other
  // destructive action on the web, and like the mobile rules screen.
  // Only a rule of your own stays deleted: the next sync puts a built-in
  // or learned rule back, so for those the prompt points at Disable.
  const confirmDelete = (m: string) => {
    if (window.confirm(source === "user"
        ? `Delete the rule for "${m}"? It stops auto-categorizing this `
          + "merchant; existing transactions keep their category."
        : `Delete the rule for "${m}"? The next sync adds it back — `
          + "Disable stops it for good."))
      del.mutate(m);
  };
  // the chip's write: the new category (and its promotion) on the row now;
  // the ledger rows it re-sorted are refetched wherever they are shown
  const onEdit = (m: string, cat: string) => {
    if (source === "user")
      patchList<RulesPage, RuleRow>(qc, key, "rules",
        (r) => r.merchant === m, { category_primary: cat, source: "user" });
    else
      removeFromList<RulesPage, RuleRow>(qc, key, "rules",
        (r) => r.merchant === m);
    refetch();
    qc.invalidateQueries({ queryKey: ["txns"] });
    qc.invalidateQueries({ queryKey: ["today"] });
    qc.invalidateQueries({ queryKey: ["merchant-detail"] });
    invalidateRollups(qc);
  };
  const d: RulesPage | undefined = rq.data;
  const pages = d ? Math.max(1, Math.ceil(d.total / d.per_page)) : 1;
  return (
    // the group carries its own explanation — the promotion rule only
    // matters where it applies, not in a shared paragraph up top
    <Disclosure open={open} onToggle={setOpen}
      summary={<><b>{title}</b>
        <span className="sub">{count.toLocaleString()} — {note}</span></>}>
      {open && rq.isPending && <p className="mut">loading…</p>}
      {open && rq.data === undefined && rq.isError &&
        <FailedLine q={rq} what="these rules" />}
      {/* the category picker in each row reads this list: an unread one
          is not "no categories" */}
      {open && d && mayEdit && cats.data === undefined && cats.isError && (
        <FailedLine q={cats} what="the categories" />)}
      {open && d && (<>
        <table className="rules-table" style={{ width: "100%" }}>
          <thead>
            <tr>
              <th>Merchant</th>
              <th>Category</th>
              <th className="num hide-m">Matches</th>
              {mayEdit && <th style={{ width: "1%" }}><span className="sr-only">Actions</span></th>}
            </tr>
          </thead>
          <tbody>
            {d.rules.map((r) => (
              <RuleLine key={r.merchant} r={r} mayEdit={mayEdit} demo={demo}
                        cats={cats.data} onEdit={onEdit}
                        onToggle={() => disable.mutate(
                          { m: r.merchant, d: !r.disabled })}
                        onDelete={() => confirmDelete(r.merchant)}
                        busy={(disable.isPending && disable.variables.m === r.merchant)
                          || (del.isPending && del.variables === r.merchant)} />
            ))}
            {d.rules.length === 0 && (
              <tr><td colSpan={mayEdit ? 4 : 3} className="mut">
                {q ? "No matches in this group."
                  : "No rules here yet. Change a transaction's category and choose to remember it, and the rule appears here."}
              </td></tr>
            )}
          </tbody>
        </table>
        <div className="sub" style={{ marginTop: ".5rem" }}>
          showing {d.rules.length} of {d.total}
          {pages > 1 && (<>
            {" · "}
            <button disabled={page <= 1}
                    onClick={() => setPage(page - 1)}>‹ Prev</button>
            {" "}page {d.page} of {pages}{" "}
            <button disabled={page >= pages}
                    onClick={() => setPage(page + 1)}>Next ›</button>
          </>)}
        </div>
      </>)}
    </Disclosure>
  );
}

function RuleLine({ r, mayEdit, demo, cats, onEdit, onToggle, onDelete, busy }: {
  r: RuleRow; mayEdit: boolean; demo: boolean; cats: Cats | undefined;
  onEdit: (merchant: string, category: string) => void;
  onToggle: () => void; onDelete: () => void; busy: boolean;
}) {
  const [menu, setMenu] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // Editing a category is the same act as recategorizing a transaction, so
  // it is the same control — a dotted-underline chip that IS the picker,
  // rather than an edit button, a free-text box and a save click. Free text
  // would also let a typo create a category; the list cannot.
  const set = async (cat: string) => {
    setErr(null); setSaving(true);
    try { await api.ruleSet(r.merchant, cat); onEdit(r.merchant, cat); }
    catch (e) { setErr(errText(e)); }
    finally { setSaving(false); }
  };
  // spend and the household's own names only — the server refuses a rule
  // that files a merchant as a transfer, income or loan payment
  const { spend, custom } = spendPickerGroups(cats);
  return (
    <>
      <tr style={{ opacity: r.disabled ? 0.55 : 1 }}>
        {/* .c-merch: a bank's run-together descriptor may break anywhere
            on a wide table (unbroken, one long token sets its width past
            the card); stacked at phone width it breaks only between words
            unless one word is wider than the line (index.css) */}
        <td className="c-merch"><b>{r.merchant}</b>
          {r.disabled && <span className="pill m"
            style={{ marginLeft: ".4rem" }}
            title="kept, but not applied to new transactions">off</span>}</td>
        <td className="c-cat">
          {mayEdit ? (
            <select className="cat-chip" value="" style={{ maxWidth: "100%" }}
                    aria-label={`category for ${r.merchant} — change`}
                    disabled={saving || demo}
                    onChange={(e) => e.target.value && set(e.target.value)}>
              <option value="">{saving ? "saving…"
                : catLabel(r.category_primary)}</option>
              <optgroup label="Spending">
                {spend.map((c) => (
                  <option key={c} value={c}>{catLabel(c)}</option>))}
              </optgroup>
              {custom.length > 0 && (
                <optgroup label="Your own categories">
                  {custom.map((c) => (
                    <option key={c} value={c}>{catLabel(c)}</option>))}
                </optgroup>)}
            </select>
          ) : (
            <span className="sub">{catLabel(r.category_primary)}</span>
          )}
          {err && <Note tone="bad" onDismiss={() => setErr(null)}>{err}</Note>}
        </td>
        <td className="num mut hide-m">{r.disabled ? "—" : r.count}</td>
        {mayEdit && (
          <td className="c-act">
            <button className="row-menu" aria-expanded={menu}
                    aria-label={`actions for ${r.merchant}`}
                    onClick={() => setMenu(!menu)}>⋯</button>
          </td>
        )}
      </tr>
      {mayEdit && menu && (
        <tr className="tr-expand"><td colSpan={4}>
          <div className="row-actions">
            <button disabled={busy || demo} onClick={onToggle}
              title={r.disabled
                ? "apply this rule again"
                : "keep the rule but stop applying it to new transactions"}>
              {r.disabled ? "Turn back on" : "Turn off"}</button>
            <button disabled={busy || demo} onClick={onDelete}
              title="the merchant can be classified again from scratch">
              Delete this rule</button>
            <span className="sub" style={{ alignSelf: "center" }}>
              {SOURCE_LABEL[r.source] ?? r.source}</span>
          </div>
        </td></tr>
      )}
    </>
  );
}
