import { type ReactNode, useMemo, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api, catLabel, errText, type SplitPart, type Txn } from "../api/client";
import { patchQueries } from "../api/cache";

// One charge, several categories — the editor for a row's hand split.
// Parts are category + amount and must add up to the charge to the cent;
// the remainder is shown live and the save is refused (here and by the
// server) until it is zero. Saving replaces the whole split; "remove
// split" hands the row back whole. The category rollups (budget, Cash
// Flow, lenses, Today) read the parts, so the queries behind those
// figures are invalidated on every write.

const cents = (v: number) => Math.round(v * 100);
const fmt = (c: number) => (c / 100).toFixed(2);

type Draft = { category: string; amount: string };

export default function SplitPanel({ t, catGroups, known, onDone }:
  { t: Txn; catGroups: ReactNode; known: Set<string>; onDone: () => void }) {
  const qc = useQueryClient();
  // the row's sign is the parts' sign: a refund splits in its own direction
  const sign = t.amount < 0 ? -1 : 1;
  const total = Math.abs(cents(t.amount));
  const [parts, setParts] = useState<Draft[]>(() => t.split?.length
    ? t.split.map((p) => ({ category: p.category, amount: fmt(Math.abs(cents(p.amount))) }))
    // start from the row's own category and one empty line, so a split
    // is "what else was in this charge" rather than a blank form
    : [{ category: t.category_override || t.category_key || "", amount: fmt(total) },
       { category: "", amount: "" }]);
  const sum = useMemo(() => parts.reduce((a, p) => {
    const v = Number(p.amount);
    return a + (Number.isFinite(v) ? cents(v) : 0);
  }, 0), [parts]);
  const left = total - sum;
  const ready = parts.length >= 2 && left === 0
    && parts.every((p) => p.category && cents(Number(p.amount)) > 0)
    && new Set(parts.map((p) => p.category)).size === parts.length;

  const patchRow = (split: SplitPart[] | null) => {
    const fn = (x: Txn) => x.id === t.id ? { ...x, split } : x;
    patchQueries<{ rows: Txn[] }>(qc, ["txns"],
      (d) => d.rows ? { ...d, rows: d.rows.map(fn) } : d);
    patchQueries<{ txns: Txn[] }>(qc, ["bills-history"],
      (d) => d.txns ? { ...d, txns: d.txns.map(fn) } : d);
    // every per-category figure counted the parts: the verdict, the
    // Spending and Cash Flow categories, both lenses
    for (const k of (["txns", "today", "bills-history", "report",
                      "lens-month", "lens-year"] as const))
      qc.invalidateQueries({ queryKey: [k] });
  };
  const save = useMutation({
    mutationFn: () => api.txnSplitSet(t.id, parts.map((p) => ({
      category: p.category, amount: sign * Number(p.amount) })).map((p) => ({
      ...p, amount: cents(p.amount) / 100 }))),
    onSuccess: (r) => { patchRow(r.split); onDone(); },
    onError: (e) => window.dispatchEvent(new CustomEvent("oiko-toast",
      { detail: `Couldn't save the split — ${errText(e)}` })),
  });
  const clear = useMutation({
    mutationFn: () => api.txnSplitClear(t.id),
    onSuccess: () => { patchRow(null); onDone(); },
    onError: (e) => window.dispatchEvent(new CustomEvent("oiko-toast",
      { detail: `Couldn't remove the split — ${errText(e)}` })),
  });
  const busy = save.isPending || clear.isPending;
  const set = (i: number, patch: Partial<Draft>) =>
    setParts((ps) => ps.map((p, j) => j === i ? { ...p, ...patch } : p));
  // the remainder lands on this line — one tap instead of arithmetic
  const fill = (i: number) => {
    const own = cents(Number(parts[i].amount) || 0);
    set(i, { amount: fmt(Math.max(0, own + left)) });
  };
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: ".45rem",
                  padding: ".2rem 0" }}>
      <div className="sub">
        Split <b>{fmt(total)}</b> at <b>{t.payee}</b> across categories —
        the parts must add up to the charge.
      </div>
      {parts.map((p, i) => (
        <div key={i} style={{ display: "flex", gap: ".5rem", alignItems: "center",
                              flexWrap: "wrap" }}>
          <select value={p.category} disabled={busy}
                  aria-label={`category of part ${i + 1}`}
                  onChange={(e) => {
                    if (e.target.value === "__customcat__") {
                      const name = window.prompt("Custom category name:")?.trim();
                      if (name) set(i, { category: name });
                      return;
                    }
                    set(i, { category: e.target.value });
                  }}>
            <option value="">category…</option>
            {/* a custom name typed here is offered back on this line */}
            {p.category && !known.has(p.category) && (
              <option value={p.category}>{catLabel(p.category)}</option>
            )}
            <option value="__customcat__">✎ custom category…</option>
            {catGroups}
          </select>
          <input type="number" inputMode="decimal" step="0.01" min="0"
                 value={p.amount} disabled={busy} placeholder="0.00"
                 aria-label={`amount of part ${i + 1}`}
                 style={{ width: "6.5rem", textAlign: "right" }}
                 onChange={(e) => set(i, { amount: e.target.value })} />
          {left !== 0 && (
            <button type="button" className="chip-x" disabled={busy}
                    title="put the remainder on this line"
                    onClick={() => fill(i)}>← {fmt(left)}</button>
          )}
          {parts.length > 2 && (
            <button type="button" className="chip-x" disabled={busy}
                    aria-label={`remove part ${i + 1}`}
                    onClick={() => setParts((ps) => ps.filter((_, j) => j !== i))}>✕</button>
          )}
        </div>
      ))}
      <div style={{ display: "flex", gap: ".5rem", alignItems: "center",
                    flexWrap: "wrap" }}>
        <button disabled={busy} onClick={() =>
          setParts((ps) => [...ps, { category: "", amount: "" }])}>+ part</button>
        <span className="sub"
              style={left !== 0 ? { color: "var(--amber)" } : undefined}>
          {left === 0 ? "adds up" : left > 0
            ? `${fmt(left)} left to place` : `${fmt(-left)} over the charge`}
        </span>
        <span style={{ marginLeft: "auto", display: "flex", gap: ".5rem" }}>
          {t.split?.length ? (
            <button disabled={busy} onClick={() => clear.mutate()}
                    title="drop the parts — the row counts whole under its own category again">
              remove split</button>
          ) : null}
          <button disabled={busy} onClick={onDone}>Cancel</button>
          <button className="pri" disabled={busy || !ready}
                  onClick={() => save.mutate()}>
            {save.isPending ? "saving…" : "Save split"}</button>
        </span>
      </div>
    </div>
  );
}
