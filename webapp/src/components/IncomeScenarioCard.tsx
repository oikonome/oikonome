import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { api, errText, money, type Settings as SettingsData } from "../api/client";
import { saveSettings } from "../api/cache";
import { mergeIncomeScenarios, type ScenarioDraft } from "../incomescenarios";

// Named take-home scenarios; the active one, converted to a monthly
// figure, drives the plan, the dynamic variable budget and the daily
// email.
export default function IncomeScenarioCard({ data, onSaved }: {
  data: SettingsData; onSaved: () => void;
}) {
  // named scenarios, one active (an older saving/not-saving pair arrives
  // migrated into list form from the server)
  type Row = ScenarioDraft;
  const [rows, setRows] = useState<Row[]>(
    (data.income_scenarios ?? []).map((x) => ({
      name: x.name, take_home: String(x.take_home), cadence: x.cadence })));
  const [active, setActive] = useState(data.active_scenario ?? "");
  const [err, setErr] = useState<string | null>(null);
  // the copy this card was seeded from: the merge base at save time
  const baseline = useRef(data.income_scenarios ?? []);
  const baseActive = useRef(data.active_scenario ?? "");
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: async () => {
      // The server replaces the list wholesale, and these rows were
      // seeded at mount; merge against its CURRENT list so a scenario
      // added or edited on another device since is not erased.
      const cur = await api.settings();
      const body: Record<string, unknown> = {
        income_scenarios: mergeIncomeScenarios(
          rows, baseline.current, cur.income_scenarios ?? []),
      };
      // the active pick is sent only when it was changed here; omitted,
      // the server keeps whichever one is live now
      if (active !== baseActive.current) body.active_scenario = active;
      // seeds ["settings"] with the returned view as the response lands
      return saveSettings(qc, body);
    },
    onSuccess: (v) => {
      // the saved view is the new merge base, and the card shows what was
      // merged in from elsewhere rather than its own pre-save rows
      const scen = v.income_scenarios ?? [];
      baseline.current = scen;
      baseActive.current = v.active_scenario ?? "";
      setRows(scen.map((x) => ({
        name: x.name, take_home: String(x.take_home), cadence: x.cadence })));
      setActive(v.active_scenario ?? "");
      setErr(null); onSaved();
    },
    onError: (e) => setErr(errText(e)),
  });
  const upd = (i: number, patch: Partial<Row>) =>
    setRows(rows.map(
      (r, j) => (j === i ? { ...r, ...patch, touched: true } : r)));
  const activeRow = rows.find((r) => r.name === active) ?? rows[0];
  const monthly = activeRow
    ? Number(activeRow.take_home.replace(/[$,\s]/g, "")) *
      ({ weekly: 4, biweekly: 2, semimonthly: 2, monthly: 1 }[
        activeRow.cadence] ?? 2)
    : null;
  return (
    <div className="card">
      <h2>Income scenarios{" "}
        <span className="mut" style={{ fontSize: 13, fontWeight: 400 }}>
          — take-home per paycheck; the active one powers the plan</span></h2>
      {err && <div className="note bad" onClick={() => setErr(null)}>{err}</div>}
      {rows.length > 0 && (
        <table style={{ marginBottom: ".5rem" }}>
          <thead><tr><th style={{ width: "1%" }}>Active</th><th>Name</th>
            <th>Take-home</th><th>Cadence</th><th /></tr></thead>
          <tbody>
            {rows.map((r, i) => (
              <tr key={i}>
                <td style={{ textAlign: "center" }}>
                  <input type="radio" name="active-scenario"
                         checked={active === r.name}
                         onChange={() => setActive(r.name)} /></td>
                <td><input value={r.name} placeholder="full salary"
                           onChange={(e) => {
                             if (active === r.name) setActive(e.target.value);
                             upd(i, { name: e.target.value });
                           }} /></td>
                <td><input inputMode="decimal" value={r.take_home}
                           style={{ width: "6.5rem" }}
                           onChange={(e) => upd(i, { take_home: e.target.value })} /></td>
                <td>
                  <select value={r.cadence}
                          onChange={(e) => upd(i, { cadence: e.target.value })}>
                    <option value="weekly">weekly</option>
                    <option value="biweekly">biweekly</option>
                    <option value="semimonthly">semimonthly</option>
                    <option value="monthly">monthly</option>
                  </select></td>
                <td><button onClick={() =>
                      setRows(rows.filter((_, j) => j !== i))}>remove</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <button type="button" style={{ marginBottom: ".5rem" }}
              onClick={() => setRows([...rows,
                { name: "", take_home: "", cadence: "biweekly",
                  touched: true }])}>
        Add scenario</button>
      <p className="mut" style={{ fontSize: 13 }}>
        {monthly && Number.isFinite(monthly)
          ? <>Active now: <b>{money(monthly)}/mo</b> ({activeRow!.name}). </>
          : null}
        Applies to the plan, cash forecast, and daily email. No scenarios →
        the plan's own income figure is used.</p>
      <button className="pri" disabled={save.isPending}
              onClick={() => save.mutate()}>
        {save.isPending ? "saving…" : "Save income scenarios"}</button>
    </div>
  );
}
