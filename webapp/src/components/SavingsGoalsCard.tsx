// savings goals — ledger-verified progress toward named funds;
// the monthly plan reduces plan surplus + rides the forecast (never the
// daily verdict). Progress reads the same payload Today renders.
// lives on the Budget page (owner-gated) — the plan's money and
// the goals it funds belong on one page, not in Settings.
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api, errText, mmdd, money, type SavingsGoal,
         type Settings as SettingsData } from "../api/client";
import { saveSettings } from "../api/cache";

export default function SavingsGoalsCard({ data, onSaved, onDirtyChange }: {
  data: SettingsData; onSaved: () => void;
  onDirtyChange?: (dirty: boolean) => void;
}) {
  interface Row { name: string; target: string; target_date: string;
                  monthly_plan: string; account_id: string; tokens: string;
                  start_balance: string; mode: string; touched?: boolean }
  const qc = useQueryClient();
  const accts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts });
  const today = useQuery({ queryKey: ["today", "now"], queryFn: () => api.todayFull() });
  const [rows, setRows] = useState<Row[]>(
    (data.savings_goals ?? []).map((g) => ({
      name: g.name, target: String(g.target),
      target_date: g.target_date ?? "",
      monthly_plan: String(g.monthly_plan || ""),
      account_id: g.account_id ?? "",
      tokens: (g.tokens ?? []).join(", "),
      start_balance: g.start_balance ? String(g.start_balance) : "",
      mode: g.mode === "sweep" ? "sweep" : "monthly" })));
  const [err, setErr] = useState<string | null>(null);
  // mount-time server copy (save-time merge base) + the dirty signal
  // the Budget page's remount guard reads
  const baseline = useRef<SavingsGoal[]>(data.savings_goals ?? []);
  const touch = () => onDirtyChange?.(true);
  const num = (v: string) => Number(v.replace(/[$,\s]/g, "") || 0);
  const save = useMutation({
    mutationFn: async () => {
      // the goals list is replaced wholesale, but these rows were
      // seeded at mount — the planner writes the "Savings" goal and
      // may have saved since. Merge against the server's CURRENT list:
      // rows the user touched here win, untouched rows follow the live
      // copy, goals added elsewhere since mount are carried through.
      const cur = await api.settings();
      const curG = cur.savings_goals ?? [];
      const baseNames = new Set(baseline.current.map((g) => g.name));
      const curByName = new Map(curG.map((g) => [g.name, g]));
      const out: SavingsGoal[] = [];
      for (const r of rows) {
        const name = r.name.trim();
        if (!name) continue;
        if (!r.touched) {
          const live = curByName.get(name);
          if (live) { out.push(live); continue; }
          if (baseNames.has(name)) continue;   // deleted elsewhere
        }
        out.push({ name, target: num(r.target),
                   target_date: r.target_date || null,
                   monthly_plan: num(r.monthly_plan),
                   account_id: r.account_id || null,
                   tokens: r.tokens.split(",").map((t) => t.trim())
                     .filter(Boolean),
                   start_balance: num(r.start_balance),
                   mode: r.mode === "sweep" ? "sweep" : "monthly" });
      }
      for (const g of curG)
        if (!baseNames.has(g.name) && !out.some((o) => o.name === g.name))
          out.push(g);                          // added elsewhere since mount
      // seeds ["settings"] with the returned view — the planner's Savings
      // row reads the same list
      return saveSettings(qc, { savings_goals: out });
    },
    onSuccess: () => { setErr(null); onDirtyChange?.(false); onSaved(); },
    onError: (e) => setErr(errText(e)),
  });
  const upd = (i: number, patch: Partial<Row>) => {
    touch();
    setRows(rows.map(
      (r, j) => (j === i ? { ...r, ...patch, touched: true } : r)));
  };
  const prog = new Map(
    (today.data?.savings_goals ?? []).map((p) => [p.name, p]));
  return (
    <div className="card" style={{ marginTop: "1rem" }}>
      <h2>Savings goals{" "}
        <span className="mut" style={{ fontSize: 13, fontWeight: 400 }}>
          — trip fund, emergency fund; progress comes from real transfers</span></h2>
      <p className="mut">A goal watches transfers on its account (add
        match words to carve several goals out of one account). The
        monthly plan lowers the excess cash line and rides the cash forecast —
        it never makes the daily verdict go over. A sweep goal contributes
        at month end instead, and only what the month&apos;s surplus covers.</p>
      {err && <div className="note bad" onClick={() => setErr(null)}>{err}</div>}
      {rows.map((r, i) => {
        const p = prog.get(r.name);
        return (
          <div key={i} className="card" style={{ margin: ".5rem 0",
                padding: "12px 14px" }}>
            <div className="field-grid">
              <label>Name
                <input value={r.name} placeholder="Trip fund"
                       onChange={(e) => upd(i, { name: e.target.value })} /></label>
              <label>Target $
                <input inputMode="decimal" value={r.target}
                       onChange={(e) => upd(i, { target: e.target.value })} /></label>
              <label>Target date <span className="mut">(optional)</span>
                <input type="date" value={r.target_date}
                       onChange={(e) => upd(i, { target_date: e.target.value })} /></label>
              <label>Plan $/month
                <input inputMode="decimal" value={r.monthly_plan}
                       onChange={(e) => upd(i, { monthly_plan: e.target.value })} /></label>
              <label>Contribute
                <select value={r.mode}
                        onChange={(e) => upd(i, { mode: e.target.value })}>
                  <option value="monthly">every month (fixed plan)</option>
                  <option value="sweep">only when the month worked out (sweep)</option>
                </select></label>
              <label>Account
                <select value={r.account_id}
                        onChange={(e) => upd(i, { account_id: e.target.value })}>
                  <option value="">any account</option>
                  {(accts.data?.accounts ?? []).map((a) => (
                    <option key={a.id} value={a.id}>{a.name}</option>
                  ))}
                </select></label>
              <label>Match words <span className="mut">(optional, comma-sep)</span>
                <input value={r.tokens} placeholder="trip"
                       onChange={(e) => upd(i, { tokens: e.target.value })} /></label>
              <label>Starting balance $ <span className="mut">(before tracking)</span>
                <input inputMode="decimal" value={r.start_balance}
                       onChange={(e) => upd(i, { start_balance: e.target.value })} /></label>
            </div>
            {p && p.pct != null && (
              <div className="mut" style={{ fontSize: 13, marginTop: ".4rem" }}>
                {money(p.saved)} of {money(p.target)} ({p.pct}%)
                {/* A sweep goal contributes in one lump at month end, so
                    the trailing-90-day pace text reads "off pace" all
                    month on a goal that is fine. Say what this goal's
                    month actually did instead — the Today line's wording,
                    per goal, since the pooled sentence there cannot tell
                    two sweep goals apart. */}
                {p.mode === "sweep" && p.swept_this_month != null
                  ? (p.sweep_satisfied
                      ? ` — swept ${money(p.swept_this_month)} this month`
                      : ` — swept ${money(p.swept_this_month)} of `
                        + `${money(p.monthly_plan)} this month`)
                  : p.on_pace === false ? " — off pace" :
                 p.on_pace ? " — on pace" :
                 p.eta && p.pct < 100 ? ` — ~${mmdd(p.eta)}` : ""}
              </div>
            )}
            <button type="button" style={{ marginTop: ".4rem" }}
                    onClick={() => { touch();
                                     setRows(rows.filter((_, j) => j !== i)); }}>
              Remove goal</button>
          </div>
        );
      })}
      <div style={{ display: "flex", gap: ".5rem", marginTop: ".5rem" }}>
        <button type="button" onClick={() => { touch(); setRows([...rows,
          { name: "", target: "", target_date: "", monthly_plan: "",
            account_id: "", tokens: "", start_balance: "", mode: "monthly",
            touched: true }]); }}>
          Add goal</button>
        <button className="pri" disabled={save.isPending}
                onClick={() => save.mutate()}>
          {save.isPending ? "saving…" : "Save savings goals"}</button>
      </div>
    </div>
  );
}
