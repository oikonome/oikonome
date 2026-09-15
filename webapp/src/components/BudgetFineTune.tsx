import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api, errText, money, type BudgetSuggestions, type CustomBucket,
         type Settings as SettingsData, catLabel } from "../api/client";
import { saveSettings } from "../api/cache";

// editable row form of a config custom_bucket: numbers and the
// merchant list are strings while being typed. `touched` marks rows the
// user actually edited here — the save-time merge lets untouched rows
// follow the server's live copy.
interface BucketRow {
  name: string; parent: "food" | "other"; monthly: string;
  categories: string[]; merchants: string; touched?: boolean;
}

// the precision controls the planner deliberately doesn't carry.
// The planner up-page owns the money model (income → bills → categories →
// savings) and edits "Everything else" carve-outs as plain rows; this card
// is the escape hatch for what that shape can't express: buckets that carve
// out of FOOD, and per-bucket category/merchant matching. Collapsed by
// default — reach for it only when the plan itself isn't enough.
export default function BudgetFineTune({ data, onSaved, onDirtyChange }: {
  data: SettingsData; onSaved: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const qc = useQueryClient();
  const cats = useQuery({ queryKey: ["categories"], queryFn: api.categories });
  const [err, setErr] = useState<string | null>(null);
  // which bucket's editor is open — one at a time
  const [editing, setEditing] = useState<number | null>(null);
  const [sugg, setSugg] = useState<BudgetSuggestions | null>(null);
  const suggest = useMutation({
    mutationFn: api.budgetsSuggest,
    onSuccess: setSugg,
    onError: (e) => setErr(errText(e)),
  });
  const [dynamic, setDynamic] = useState(!!data.dynamic_variable_budget);
  const [buckets, setBuckets] = useState<BucketRow[]>(
    (data.custom_buckets ?? []).map((b) => ({
      name: b.name, parent: b.parent,
      monthly: b.monthly ? String(b.monthly) : "",
      categories: b.categories ?? [],
      merchants: (b.merchants ?? []).join(", "),
    })));
  // the mount-time server copy (for change detection at save) and the
  // dirty signal the page's remount guard reads
  const baseline = useRef<CustomBucket[]>(data.custom_buckets ?? []);
  const touch = () => onDirtyChange?.(true);

  const save = useMutation({
    // The dynamic flag arrives as the mutation variable when the checkbox
    // is what fired the save: the handler calls setDynamic and then mutate
    // in the same tick, so reading `dynamic` from this closure sent the
    // PREVIOUS render's value — the box flipped on screen and the server
    // stored the opposite.
    mutationFn: async (dyn?: boolean) => {
      const num = (s: string) => Number(s.replace(/[$,\s]/g, "") || 0);
      // partial body on purpose: food/other/income and the savings plan are
      // the planner's to write — sending them here would fight it.
      // the bucket LIST is still replaced wholesale, and these
      // rows were seeded at mount — the planner or another tab may have
      // saved since. Merge against the server's CURRENT list: rows the
      // user touched here win, untouched rows follow the live copy
      // (changed elsewhere → take theirs; deleted elsewhere → drop), and
      // buckets added elsewhere since mount are carried through.
      const cur = await api.settings();
      const curB = cur.custom_buckets ?? [];
      const baseNames = new Set(baseline.current.map((b) => b.name));
      const curByName = new Map(curB.map((b) => [b.name, b]));
      const out: CustomBucket[] = [];
      for (const b of buckets) {
        const name = b.name.trim();
        if (!name) continue;
        if (!b.touched) {
          const live = curByName.get(name);
          if (live) { out.push(live); continue; }
          if (baseNames.has(name)) continue;   // deleted elsewhere
        }
        out.push({ name, parent: b.parent, monthly: num(b.monthly),
                   categories: b.categories,
                   merchants: b.merchants.split(",").map((m) => m.trim())
                     .filter(Boolean) });
      }
      for (const b of curB)
        if (!baseNames.has(b.name) && !out.some((o) => o.name === b.name))
          out.push(b);                          // added elsewhere since mount
      // returns the full settings view and seeds the cache with it, so the
      // page's other cards see the new buckets as the response lands
      return saveSettings(qc, {
        dynamic_variable_budget: dyn ?? dynamic,
        custom_buckets: out,
      });
    },
    onSuccess: () => { setErr(null); onDirtyChange?.(false); onSaved(); },
    onError: (e, dyn) => {
      setErr(errText(e));
      // the checkbox flipped before the request went out — put it back
      if (dyn !== undefined) setDynamic(!dyn);
    },
  });

  const setBucket = (i: number, patch: Partial<BucketRow>) => {
    touch();
    setBuckets(buckets.map(
      (b, j) => (j === i ? { ...b, ...patch, touched: true } : b)));
  };
  const addBucket = () => {
    touch();
    setBuckets([...buckets, { name: "", parent: "other", monthly: "",
                              categories: [], merchants: "",
                              touched: true }]);
  };
  const delBucket = (i: number) => {
    touch();
    setBuckets(buckets.filter((_, j) => j !== i));
  };

  // "Suggest from history": each number is shown next to its field and
  // only lands when the user clicks "use" — approve each, never in bulk
  const chip = (v: number | undefined, apply: (s: string) => void) =>
    sugg && v !== undefined ? (
      <span className="mut" style={{ fontSize: 12, whiteSpace: "nowrap" }}>
        {" "}suggested {money(v)}{" "}
        <button type="button" style={{ fontSize: 12, padding: "0 .4rem" }}
                onClick={() => apply(String(v))}>use</button>
      </span>
    ) : null;

  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Bucket rules
        <span className="mut" style={{ fontSize: 13, fontWeight: 400 }}>
          {" "}— what counts toward each bucket; a merchant match wins</span></h2>
      {err && <div className="note bad" onClick={() => setErr(null)}>{err}</div>}

      {buckets.length === 0 && (
        <p className="sub">No buckets yet — a bucket carves named spend out of
          Food or Everything else, and shows as its own bar on Today.</p>
      )}

      {buckets.map((b, i) => (
        editing === i ? (
          /* ONE editor at a time, three calm lines. A permanent five-field
             grid per bucket would need a ⌘/Ctrl-click multiselect for
             categories — the exact keyboard-and-hover dependency the native
             apps cannot carry — and a merchant field of raw delimited text
             where a typo is invisible. */
          <div key={i} style={{ background: "var(--hover)",
                borderRadius: "var(--rs)", padding: ".6rem .8rem",
                margin: ".4rem 0" }}>
            <div style={{ display: "flex", gap: ".6rem", flexWrap: "wrap",
                          alignItems: "center" }}>
              <input value={b.name} placeholder="Coffee" size={12}
                     aria-label="bucket name"
                     onChange={(e) => setBucket(i, { name: e.target.value })} />
              <select value={b.parent} aria-label="carves out of"
                      onChange={(e) => setBucket(
                        i, { parent: e.target.value as BucketRow["parent"] })}>
                <option value="other">from Everything else</option>
                <option value="food">from Food</option>
              </select>
              <label className="sub">$ <input inputMode="decimal" size={6}
                     value={b.monthly} aria-label="monthly budget"
                     onChange={(e) => setBucket(i, { monthly: e.target.value })} />
                {" "}/mo</label>
              {chip(sugg?.suggestions.buckets[b.name.trim()],
                    (v) => setBucket(i, { monthly: v }))}
            </div>
            {/* categories and merchants share ONE chip row with one ＋: they
                are both "things that count toward this bucket", and the rule
                that a merchant match beats a category is said once, here */}
            <div style={{ display: "flex", gap: ".4rem", flexWrap: "wrap",
                          alignItems: "center", marginTop: ".55rem" }}>
              {b.categories.map((c) => (
                <span key={c} className="pill b">{catLabel(c)}{" "}
                  <button aria-label={`remove ${c}`} className="chip-x"
                          onClick={() => setBucket(i, { categories:
                            b.categories.filter((x) => x !== c) })}>✕</button>
                </span>
              ))}
              {b.merchants.split(",").map((m) => m.trim()).filter(Boolean)
                .map((m) => (
                <span key={m} className="pill b">{m}{" "}
                  <button aria-label={`remove ${m}`} className="chip-x"
                          onClick={() => setBucket(i, { merchants:
                            b.merchants.split(",").map((x) => x.trim())
                              .filter((x) => x && x !== m).join(", ") })}>✕</button>
                </span>
              ))}
              <select value="" className="addchip"
                      aria-label="add a category"
                      onChange={(e) => {
                        if (!e.target.value) return;
                        setBucket(i, { categories: [...b.categories,
                                                    e.target.value] });
                      }}>
                <option value="">＋ category</option>
                {(cats.data?.categories ?? [])
                  .filter((c) => !b.categories.includes(c))
                  .map((c) => <option key={c} value={c}>{catLabel(c)}</option>)}
              </select>
              <input placeholder="＋ merchant, then Enter" size={16}
                     aria-label="add a merchant"
                     style={{ fontSize: 12 }}
                     onKeyDown={(e) => {
                       if (e.key !== "Enter") return;
                       e.preventDefault();
                       const v = (e.target as HTMLInputElement).value.trim();
                       if (!v) return;
                       setBucket(i, { merchants: [b.merchants, v]
                         .filter(Boolean).join(", ") });
                       (e.target as HTMLInputElement).value = "";
                     }} />
            </div>
            <div style={{ display: "flex", gap: ".5rem", alignItems: "center",
                          marginTop: ".6rem" }}>
              <button className="pri" disabled={save.isPending}
                      onClick={() => { save.mutate(undefined); setEditing(null); }}>
                {save.isPending ? "saving…" : "save"}</button>
              <button onClick={() => setEditing(null)}>cancel</button>
              <button style={{ marginLeft: "auto", border: "none",
                               background: "none", color: "var(--mut)" }}
                      onClick={() => { delBucket(i); setEditing(null); }}>
                remove</button>
            </div>
          </div>
        ) : (
          /* the whole rule on one quiet line — the card is scannable without
             opening anything */
          <div key={i} style={{ display: "flex", gap: ".7rem",
                alignItems: "baseline", flexWrap: "wrap", padding: ".5rem 0",
                borderBottom: "1px solid var(--line)" }}>
            <b>{b.name || "(unnamed)"}</b>
            <span className="sub">{money(Number(b.monthly) || 0)}/mo from{" "}
              {b.parent === "food" ? "Food" : "Everything else"}</span>
            <span className="sub" style={{ opacity: .85, minWidth: 0,
                  overflow: "hidden", textOverflow: "ellipsis",
                  whiteSpace: "nowrap" }}>
              {[...b.categories.map(catLabel),
                ...b.merchants.split(",").map((m) => m.trim()).filter(Boolean)]
                .join(" · ") || "nothing matched yet"}</span>
            <button className="row-menu" style={{ marginLeft: "auto" }}
                    aria-label={`edit ${b.name || "bucket"}`}
                    onClick={() => setEditing(i)}>✎</button>
          </div>
        )
      ))}

      <div style={{ display: "flex", gap: ".8rem", alignItems: "center",
                    flexWrap: "wrap", marginTop: ".7rem" }}>
        <button onClick={() => { addBucket(); setEditing(buckets.length); }}>
          + add bucket</button>
        <button disabled={suggest.isPending}
                onClick={() => suggest.mutate()}>
          {suggest.isPending ? "computing…" : "Suggest from history"}</button>
        <label className="sub" style={{ marginLeft: "auto", display: "flex",
              gap: ".4rem", alignItems: "center" }}>
          <input type="checkbox" checked={dynamic} disabled={save.isPending}
                 onChange={(e) => { touch(); setDynamic(e.target.checked);
                                    save.mutate(e.target.checked); }} />
          dynamic budget <span className="mut">— adapts to the month so far</span>
        </label>
      </div>
      {sugg && (
        <p className="mut" style={{ fontSize: 13, marginTop: ".4rem" }}>
          Suggestions are the median of the last {sugg.months.length} months,
          this one so far included (outlier months excluded). Click “use”
          beside a bucket's
          amount to take one.</p>
      )}
    </div>
  );
}
