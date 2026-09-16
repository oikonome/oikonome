// the learned-rules surface. Every categorization rule the system
// applies — with provenance (your correction / model / seed) — visible and
// controllable: change the category from the same chip the ledger uses, or
// turn a rule off / delete it from its ⋯ strip. No hand-authoring: rules are
// learned; this page just shows and corrects them. Owner-only edits; viewers
// see the list read-only.
//
// One section per provenance — "Your
// rules" expanded, Model-learned / Learned / Built-in collapsed to a count. Groups
// load LAZILY with server-side search + pagination (~50/page; a real
// ledger has thousands of rules and nothing may render them all at once).
// Editing or disabling a model/seed rule PROMOTES it to source='user' and
// it moves to "Your rules" — the Model group stays purely untouched
// inferences, and it hides entirely at zero (an instance with no LLM
// never shows it). No group-level kill switch; per-rule disable only.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api, errText,  type RuleRow, type RulesPage, catLabel } from "../api/client";
import { patchList, removeFromList } from "../api/cache";
import { canEdit } from "../role";

const SOURCE_LABEL: Record<string, string> = {
  user: "your correction",
  llm: "classified by model",
  seed: "matched seed rule",
  model: "learned from your corrections",
};
type Source = "user" | "llm" | "seed" | "model";
type Cats = Awaited<ReturnType<typeof api.categories>>;

export default function Rules({ embedded = false }:
                                   { embedded?: boolean } = {}) {
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  const [filter, setFilter] = useState("");
  const [q, setQ] = useState("");   // debounced — server-side search
  useEffect(() => {
    const t = window.setTimeout(() => setQ(filter.trim()), 300);
    return () => window.clearTimeout(t);
  }, [filter]);

  // "Your rules" is the always-loaded group; its response also carries the
  // q-filtered per-source counts the collapsed headers need — no rows load
  // for a group until it's expanded.
  const userQ = useQuery({ queryKey: ["rules", "user", q, 1],
                           queryFn: () => api.rules("user", q, 1) });
  const counts = userQ.data?.counts;
  // all FOUR provenances count — the trained classifier ("model") is a
  // source like any other, and leaving it out would both undercount the
  // header and let a model-only ledger render as "no rules yet".
  const total = counts
    ? counts.user + counts.llm + counts.seed + (counts.model ?? 0) : 0;
  const empty = !q && !!counts && total === 0;

  if (userQ.isPending)
    return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (userQ.isError)
    return <div className="note bad">Couldn't load: {String(userQ.error)}</div>;

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
            {total.toLocaleString()} rule{total === 1 ? "" : "s"}</span>
          <span className="sub">sorting your transactions
            {counts && <> · {counts.user} yours · {counts.llm} model
              {(counts.model ?? 0) > 0 && <> · {counts.model} learned</>}
              · {counts.seed} built-in</>}</span>
          {!empty && (
            <input value={filter} onChange={(e) => setFilter(e.target.value)}
                   placeholder="search merchants or categories"
                   style={{ marginLeft: "auto", minWidth: "15rem" }} />)}
        </div>
        <div className="sub" style={{ marginTop: ".2rem" }}>Rules are learned,
          never hand-written. Correcting or disabling one makes it yours.</div>
      </div>

      {empty ? (
        <div className={embedded ? undefined : "card"}>
          <p className="mut" style={{ margin: 0 }}>
            No rules yet — they appear as Oikonome learns your merchants:
            built-in rules match on your first import, the model classifies
            the rest when an LLM is configured (Settings), and every category
            correction you make becomes a rule of your own.
          </p>
        </div>
      ) : (<>
        <div className={embedded ? undefined : "card"}>
          <RuleGroup source="user" title="Your rules" mayEdit={mayEdit} q={q}
                     note="corrections you made"
                     count={counts?.user ?? 0} defaultOpen />
        </div>
        <div className={embedded ? undefined : "card"}>
          {(counts?.llm ?? 0) > 0 && (
            <RuleGroup source="llm" title="Model-learned" mayEdit={mayEdit} q={q}
                       note="inferred by the local model; correcting one makes it yours"
                       count={counts?.llm ?? 0} />)}
          {(counts?.model ?? 0) > 0 && (
            <RuleGroup source="model" title="Learned" mayEdit={mayEdit} q={q}
                       note="the classifier trained on your own corrections; correcting one makes it yours"
                       count={counts?.model ?? 0} />)}
          {(counts?.seed ?? 0) > 0 && (
            <RuleGroup source="seed" title="Built-in" mayEdit={mayEdit} q={q}
                       note="shipped starting points"
                       count={counts?.seed ?? 0} />)}
        </div>
      </>)}

      {/* the page's rarest action, below its content */}
      {mayEdit && (
        <details className={embedded ? undefined : "card"}>
          <summary style={{ cursor: "pointer", color: "var(--mut)",
                            fontSize: 13.5 }}>
            Rename a custom category…</summary>
          <RenameCategoryCard />
        </details>
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
        <select value={from} onChange={(e) => setFrom(e.target.value)}
                style={{ minWidth: "12rem" }}>
          <option value="">
            {options.length ? "your custom name…" : "no custom categories yet"}
          </option>
          {options.map((c) => (
            <option key={c} value={c}>{catLabel(c)}</option>
          ))}
        </select>
        <span className="mut">→</span>
        <input value={to} placeholder="new name"
               onChange={(e) => setTo(e.target.value)}
               style={{ minWidth: "12rem" }} />
        <button className="pri"
                disabled={rename.isPending || !from.trim() || !to.trim()
                  || from.trim() === to.trim()}
                onClick={() => rename.mutate()}>
          {rename.isPending ? "renaming…" : "Rename"}
        </button>
      </div>
      {msg && <div className="note good" style={{ marginTop: ".5rem" }}>{msg}</div>}
      {err && <div className="note bad" style={{ marginTop: ".5rem" }}
                   onClick={() => setErr(null)}>{err}</div>}
    </div>
  );
}

function RuleGroup({ source, title, mayEdit, q, count, note,
                    defaultOpen = false }: {
  source: Source; title: string; mayEdit: boolean; q: string; note: string;
  count: number; defaultOpen?: boolean;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(defaultOpen);
  const [page, setPage] = useState(1);
  useEffect(() => setPage(1), [q]);     // a new search restarts at page 1
  const key = ["rules", source, q, page];
  const rq = useQuery({ queryKey: key,
                        queryFn: () => api.rules(source, q, page),
                        enabled: open });
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
    },
  });
  const del = useMutation({
    mutationFn: (m: string) => api.ruleDelete(m),
    onSuccess: (_r, m) => {
      removeFromList<RulesPage, RuleRow>(qc, key, "rules",
        (r) => r.merchant === m);
      refetch();
    },
  });
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
  };
  const d: RulesPage | undefined = rq.data;
  const pages = d ? Math.max(1, Math.ceil(d.total / d.per_page)) : 1;
  return (
    <div>
      {/* the group carries its own explanation — the promotion rule only
          matters where it applies, not in a shared paragraph up top */}
      <button className="grouphead" onClick={() => setOpen(!open)}
              aria-expanded={open}>
        <b>{title}</b>
        <span className="sub">{count} — {note}</span>
        <span className="sub" style={{ marginLeft: "auto" }}>
          {open ? "▾" : "▸"}</span>
      </button>
      {open && rq.isPending && <p className="mut">loading…</p>}
      {open && rq.isError &&
        <div className="note bad">Couldn't load: {String(rq.error)}</div>}
      {open && d && (<>
        <table style={{ width: "100%" }}>
          <thead>
            <tr>
              <th>Merchant</th>
              <th>Category</th>
              <th className="num hide-m">Matches</th>
              {mayEdit && <th style={{ width: "1%" }}></th>}
            </tr>
          </thead>
          <tbody>
            {d.rules.map((r) => (
              <RuleLine key={r.merchant} r={r} mayEdit={mayEdit}
                        cats={cats.data} onEdit={onEdit}
                        onToggle={() => disable.mutate(
                          { m: r.merchant, d: !r.disabled })}
                        onDelete={() => del.mutate(r.merchant)}
                        busy={(disable.isPending && disable.variables.m === r.merchant)
                          || (del.isPending && del.variables === r.merchant)} />
            ))}
            {d.rules.length === 0 && (
              <tr><td colSpan={mayEdit ? 4 : 3} className="mut">
                {q ? "No matches in this group." : "No rules here yet."}
              </td></tr>
            )}
          </tbody>
        </table>
        <div className="sub" style={{ marginTop: ".5rem" }}>
          showing {d.rules.length} of {d.total}
          {pages > 1 && (<>
            {" · "}
            <button disabled={page <= 1}
                    onClick={() => setPage(page - 1)}>‹ prev</button>
            {" "}page {d.page} of {pages}{" "}
            <button disabled={page >= pages}
                    onClick={() => setPage(page + 1)}>next ›</button>
          </>)}
        </div>
      </>)}
    </div>
  );
}

function RuleLine({ r, mayEdit, cats, onEdit, onToggle, onDelete, busy }: {
  r: RuleRow; mayEdit: boolean; cats: Cats | undefined;
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
  const custom = (cats?.categories ?? []).filter((c) => c
    && !(cats?.plaid_spend ?? []).includes(c)
    && !(cats?.plaid_flow ?? []).includes(c));
  return (
    <>
      <tr style={{ opacity: r.disabled ? 0.55 : 1 }}>
        <td><b>{r.merchant}</b>
          {r.disabled && <span className="pill m"
            style={{ marginLeft: ".4rem" }}
            title="kept, but not applied to new transactions">off</span>}</td>
        <td>
          {mayEdit ? (
            <select className="cat-chip" value=""
                    aria-label={`category for ${r.merchant} — change`}
                    disabled={saving}
                    onChange={(e) => e.target.value && set(e.target.value)}>
              <option value="">{saving ? "saving…"
                : catLabel(r.category_primary)}</option>
              <optgroup label="Spending">
                {cats?.plaid_spend.map((c) => (
                  <option key={c} value={c}>{catLabel(c)}</option>))}
              </optgroup>
              <optgroup label="Transfers &amp; income">
                {cats?.plaid_flow.map((c) => (
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
          {err && <div className="note bad" onClick={() => setErr(null)}>{err}</div>}
        </td>
        <td className="num mut hide-m">{r.disabled ? "—" : r.count}</td>
        {mayEdit && (
          <td>
            <button className="row-menu" aria-expanded={menu}
                    aria-label={`actions for ${r.merchant}`}
                    onClick={() => setMenu(!menu)}>⋯</button>
          </td>
        )}
      </tr>
      {mayEdit && menu && (
        <tr className="tr-expand"><td colSpan={4}>
          <div className="row-actions">
            <button disabled={busy} onClick={onToggle}
              title={r.disabled
                ? "apply this rule again"
                : "keep the rule but stop applying it to new transactions"}>
              {r.disabled ? "turn back on" : "turn off"}</button>
            <button disabled={busy} onClick={onDelete}
              title="the merchant can be classified again from scratch">
              delete this rule</button>
            <span className="sub" style={{ alignSelf: "center" }}>
              {SOURCE_LABEL[r.source] ?? r.source}</span>
          </div>
        </td></tr>
      )}
    </>
  );
}
