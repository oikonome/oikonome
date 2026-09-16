import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api, mmdd, type AlertRow } from "../api/client";
import { patchList } from "../api/cache";
import { canEdit } from "../role";

const SEV_PILL: Record<string, string> = { good: "g", warn: "a", bad: "r", info: "m" };
const sevIcon = (s: string) => (s === "good" ? "✓" : s === "info" ? "•" : "⚠");
const sevNote = (s: string) => (s === "bad" ? "bad" : s === "good" ? "good" : s === "info" ? "info" : "");
// history filters — a six-month log needs them, and "dismissed" stops being
// a state you have to scan the whole table for
const FILTERS = ["all", "connection", "bills", "cash", "dismissed"] as const;
type Filter = typeof FILTERS[number];
// which alert kinds each chip covers; anything unmapped falls under "all"
// Where each kind is fixed. The LIVE alert carries its own `link` (Today's
// strip uses it), but /alerts/history stores only the identity columns —
// link is deliberately not one of them, so retargeting cannot resurrect a
// dismissed alert. This map is the fallback for history rows.
const KIND_ROUTE: Record<string, string> = {
  "stale-pull": "/accounts", "connection-removed": "/accounts",
  setup: "/bills", proposals: "/bills", drift: "/bills", merges: "/merchants",
  reimb: "/reimburse", funding: "/networth", transfer: "/transactions",
  anomaly: "/transactions", bonus: "/transactions",
};
// "fix this" is the right verb for something BROKEN — a dead connection, a
// funding gap. It is the wrong verb for a queue: a detected bill waiting on
// a yes/no is not a fault, and calling it one makes routine review sound
// like breakage.
const KIND_CTA: Record<string, string> = {
  proposals: "review", drift: "review", merges: "review", anomaly: "review",
  bonus: "review", reimb: "review", transfer: "review",
};
const FILTER_KINDS: Record<string, string[]> = {
  connection: ["stale-pull", "connection-removed", "setup"],
  bills: ["proposals", "drift"],
  cash: ["funding", "transfer", "bonus", "anomaly", "reimb"],
};

export default function Alerts() {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["alertsHistory"], queryFn: api.alertsHistory });
  // dismiss/restore are tenant-wide writes — viewers read only
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  const [filter, setFilter] = useState<Filter>("all");

  type Hist = { alerts: AlertRow[] };
  const act = useMutation({
    mutationFn: (v: { row: AlertRow; restore: boolean }) =>
      v.restore
        ? api.alertRestore(v.row.kind, v.row.message)
        : api.alertDismiss(v.row.kind, v.row.message),
    // Dismiss and restore have exactly one outcome — the flag flips — so
    // flip it in the cache before the request goes out: the row moves
    // sections and the header bell recounts in the same frame, instead of
    // both sitting there until a refetch of the whole six-month log lands.
    // The snapshot goes back only if the server refuses.
    onMutate: async (v) => {
      await qc.cancelQueries({ queryKey: ["alertsHistory"] });
      const prev = qc.getQueryData<Hist>(["alertsHistory"]);
      patchList<Hist, AlertRow>(qc, ["alertsHistory"], "alerts",
        (a) => a.kind === v.row.kind && a.message === v.row.message,
        { dismissed: v.restore ? 0 : 1 });
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["alertsHistory"], ctx.prev);
    },
    // Today's strip reads the live alerts, which a dismissal does change
    onSuccess: () => qc.invalidateQueries({ queryKey: ["today"] }),
  });
  // only the row being written is busy — one click must not grey every
  // other row's button while it lands
  const busy = (r: AlertRow) => act.isPending &&
    act.variables?.row.kind === r.kind && act.variables.row.message === r.message;

  if (q.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;
  if (q.isError)
    return <div className="card">Couldn't load alerts: {String(q.error)}</div>;

  const rows = [...q.data.alerts].sort((a, b) =>
    a.last_seen < b.last_seen ? 1 : a.last_seen > b.last_seen ? -1 : 0);
  const active = rows.filter((r) => r.active && !r.dismissed);

  const shown = rows.filter((r) =>
    filter === "all" ? true
      : filter === "dismissed" ? (r.active && r.dismissed)
      : (FILTER_KINDS[filter] ?? []).includes(r.kind));
  const dismissed = rows.filter((r) => r.active && r.dismissed);
  const resolved = rows.filter((r) => !r.active);

  return (
    <>
      <h1>Alerts</h1>
      {/* HERO: the page's state, which a bare "Active alerts" heading
          does not say. */}
      <div className="card" style={{ marginTop: 0 }}>
        <div style={{ display: "flex", gap: ".8rem", alignItems: "baseline",
                      flexWrap: "wrap" }}>
          <span style={{ fontSize: "1.25rem", fontWeight: 700 }}>
            {active.length === 0 ? "All clear"
              : `${active.length} need${active.length === 1 ? "s" : ""} attention`}
          </span>
          <span className="sub">
            {dismissed.length > 0 && `${dismissed.length} dismissed · `}
            {resolved.length} resolved
          </span>
        </div>
      </div>

      {active.length > 0 && (
        <div className="card">
          {active.map((a) => (
            <div key={`${a.kind}|${a.message}`}
                 className={`alertrow ${sevNote(a.severity)}`}>
              <span className="ico">{sevIcon(a.severity)}</span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div>{a.message}</div>
                <div className="meta">{a.kind.replace(/-/g, " ")}
                  {a.first_seen && ` · first seen ${mmdd(a.first_seen)}`}</div>
              </div>
              <div style={{ display: "flex", gap: ".4rem", flexShrink: 0 }}>
                {/* ✕ dismiss alone is the wrong single affordance: the
                    thing a person actually wants is to FIX it, and without
                    a link that means reading the message and guessing the
                    page. */}
                {(a.link || KIND_ROUTE[a.kind]) && (
                  <Link className="btn" to={a.link || KIND_ROUTE[a.kind]}
                        style={{ textDecoration: "none", fontSize: 12.5,
                                 borderColor: "var(--blue)",
                                 color: "var(--blue)" }}>
                    {KIND_CTA[a.kind] || "fix this"} ›</Link>
                )}
                {mayEdit && (
                  <button className="dismiss" title="dismiss"
                          disabled={busy(a)}
                          onClick={() => act.mutate({ row: a, restore: false })}>
                    ✕</button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="card">
        <div style={{ display: "flex", gap: ".8rem", alignItems: "baseline",
                      flexWrap: "wrap" }}>
          <h2 style={{ margin: 0 }}>History</h2>
          {/* a six-month log is unreadable without these, and "dismissed"
              stops being a state you have to scan for */}
          <span style={{ marginLeft: "auto", display: "flex", gap: ".35rem",
                         flexWrap: "wrap" }}>
            {FILTERS.map((f) => (
              <button key={f} className={"chip" + (filter === f ? " on" : "")}
                      onClick={() => setFilter(f)}>{f}</button>
            ))}
          </span>
        </div>
        {rows.length === 0 ? (
          <p className="sub" style={{ marginBottom: 0 }}>No alerts logged yet —
            they appear here as the daily checks find things worth telling
            you about.</p>
        ) : (
          <table style={{ marginTop: ".5rem" }}>
            <thead>
              <tr>
                <th style={{ width: "1%", whiteSpace: "nowrap" }}>When</th>
                <th style={{ width: "1%" }}>Kind</th>
                <th>Alert</th>
                <th style={{ width: "1%" }}></th>
              </tr>
            </thead>
            <tbody>
              {shown.map((r) => (
                <tr key={`${r.kind}|${r.message}`}
                    style={r.dismissed ? { opacity: .6 } : undefined}>
                  {/* ONE "when" column. Split into first-seen and
                      last-seen, the first is hide-m — so a phone loses the
                      AGE of a problem, which is most of its severity. */}
                  <td className="mut" style={{ whiteSpace: "nowrap" }}>
                    {mmdd(r.first_seen)}
                    <span className="sub"> → {r.active ? "today"
                      : mmdd(r.last_seen)}</span>
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    <span className={"pill " + (r.active
                      ? (SEV_PILL[r.severity] ?? "m") : "g")}>
                      {r.active ? r.kind.replace(/-/g, " ") : "resolved"}</span>
                  </td>
                  <td>{r.message}</td>
                  <td style={{ whiteSpace: "nowrap" }}>
                    {r.active && r.dismissed ? (
                      <>
                        <span className="pill m">dismissed</span>{" "}
                        {mayEdit && (
                        <button disabled={busy(r)}
                                onClick={() => act.mutate({ row: r, restore: true })}>
                          restore</button>
                        )}
                      </>
                    ) : r.active ? <span className="pill a">active</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}
