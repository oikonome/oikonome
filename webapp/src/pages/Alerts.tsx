import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { alertAction, api, mmdd, withLiveLinks, type AlertRow,
         type TodayFull } from "../api/client";
import { patchList } from "../api/cache";
import { FailedLine } from "../components/Loaded";
import { EMPTY_LINES, localDayOf } from "../moneymath";
import { canEdit } from "../role";
import EmptyState from "../components/EmptyState";
import SegmentedControl from "../components/SegmentedControl";
import Note from "../components/Note";

const SEV_PILL: Record<string, string> = { good: "g", warn: "a", bad: "r", info: "m" };
const sevIcon = (s: string) => (s === "good" ? "✓" : s === "info" ? "•" : "⚠");
const sevNote = (s: string) => (s === "bad" ? "bad" : s === "good" ? "good" : s === "info" ? "info" : "");
// history filters — a six-month log needs them, and "dismissed" stops being
// a state you have to scan the whole table for. Which chip lists a line is
// the server's (`group`), as is where it is acted on (`link`) and what the
// history calls its kind (`kind_label`).
const FILTERS = ["all", "connection", "bills", "merchants", "cash",
                 "dismissed"] as const;
type Filter = typeof FILTERS[number];

export default function Alerts() {
  const qc = useQueryClient();
  // Read fresh on every visit, and never shown from before it: Today writes
  // a newly raised alert to the log at the end of its own (slower) request,
  // so a log the bell fetched beside Today can predate the alert Today's
  // "needs you" row just sent the person here to act on.
  // One retry, not three: a failed refresh shows the copy it has, marked.
  const q = useQuery({ queryKey: ["alertsHistory"], queryFn: api.alertsHistory,
                       refetchOnMount: "always", retry: 1 });
  // dismiss/restore are tenant-wide writes — viewers read only
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const mayEdit = canEdit(me);
  const [filter, setFilter] = useState<Filter>("all");
  // what needs attention is the page; the six-month log opens on request,
  // so three live alerts never read as three more rows of history
  const [showHistory, setShowHistory] = useState(false);

  type Hist = { alerts: AlertRow[] };
  const act = useMutation({
    // dismiss and restore answer different counts; only success matters
    mutationFn: (v: { row: AlertRow; restore: boolean }): Promise<unknown> =>
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
    // Today's strip reads the live alerts, which a dismissal does change;
    // the log is read again too, so a row Today rewrote meanwhile (a new
    // count or amount) is not hidden behind the patched one
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["today"] });
      qc.invalidateQueries({ queryKey: ["alertsHistory"] });
    },
  });
  // only the row being written is busy — one click must not grey every
  // other row's button while it lands
  const busy = (r: AlertRow) => act.isPending &&
    act.variables?.row.kind === r.kind && act.variables.row.message === r.message;

  if (q.isPending || (q.isFetching && !q.isFetchedAfterMount
                       && !q.isRefetchError))
    return <div className="centered"><span className="mut">loading…</span></div>;
  if (q.isLoadingError)
    // the page keeps its title when its read fails, as every page does
    return <><h1>Alerts</h1>
      <div className="card"><FailedLine q={q} what="alerts" /></div></>;

  // the live alerts Today already fetched carry each one's own target
  const live = qc.getQueryData<TodayFull>(["today", "now"])?.alerts;
  // one line per subject, live lines first, in the server's order
  const rows = withLiveLinks(q.data.alerts, live);
  const active = rows.filter((r) => r.active && !r.dismissed);

  const shown = rows.filter((r) =>
    filter === "all" ? true
      : filter === "dismissed" ? (r.active && r.dismissed)
      : r.group === filter);
  const dismissed = rows.filter((r) => r.active && r.dismissed);
  const resolved = rows.filter((r) => !r.active);

  return (
    <>
      <h1>Alerts</h1>
      {q.isRefetchError && (
        <Note style={{ margin: "0 0 1rem" }}>
          Couldn't refresh — showing alerts as of the last check.
        </Note>
      )}
      {/* a two-factor recovery pending on this account — the notice
          can still be undone, and only Settings > Security cancels it */}
      {me.data?.factor_reset_pending && (
        <Note tone="bad" style={{ margin: "0 0 1rem" }}>
          <span style={{ flex: 1 }}>
            Someone asked for a recovery that removes this account's
            two-factor methods. It lands {mmdd(localDayOf(me.data.factor_reset_pending))}.
            {" "}Not you?{" "}
            <Link to="/settings/security">Cancel it in Security</Link>
          </span>
        </Note>
      )}
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
                <div>{a.text}</div>
                <div className="meta">{a.kind_label ?? a.kind}
                  {a.first_seen && ` · first seen ${mmdd(a.first_seen)}`}</div>
              </div>
              <div style={{ display: "flex", gap: ".4rem", flexShrink: 0 }}>
                {/* ✕ dismiss alone is the wrong single affordance: the
                    thing a person actually wants is to FIX it, and without
                    a link that means reading the message and guessing the
                    page. */}
                {a.link && (
                  <Link className="btn" to={a.link}
                        style={{ textDecoration: "none", fontSize: 12.5,
                                 borderColor: "var(--blue)",
                                 color: "var(--blue)" }}>
                    {/* the one verb set (Fix / Review) every surface uses */}
                    {alertAction(a.kind)} ›</Link>
                )}
                {mayEdit && (
                  <button className="dismiss" title="dismiss" aria-label="Dismiss"
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
          {rows.length > 0 && (
          <button className="btn" style={{ fontSize: 12.5 }}
                  onClick={() => setShowHistory((v) => !v)}>
            {showHistory ? "hide history"
              : `show history (${rows.length} ${rows.length === 1 ? "alert" : "alerts"})`}
          </button>
          )}
          {/* a six-month log is unreadable without these, and "dismissed"
              stops being a state you have to scan for */}
          {showHistory && rows.length > 0 && (
          <SegmentedControl label="Show" size="sm" value={filter}
            onChange={setFilter} style={{ marginLeft: "auto" }}
            options={FILTERS.map((f) => ({
              value: f, label: f[0].toUpperCase() + f.slice(1) }))} />
          )}
        </div>
        {rows.length === 0 ? (
          <EmptyState title="No alerts yet"
            text="The daily checks post here when they find something worth telling you: a bill that jumped, a connection that needs you, cash running tight." />
        ) : !showHistory ? null
          // a filter that matches nothing says so, not a header over no rows
          : shown.length === 0 ? <p className="sub">{EMPTY_LINES.filtered}</p> : (
          // one stacked line per subject — the words get the full width,
          // so a phone-width page reads each alert in a few lines rather
          // than a ten-line column beside three narrow ones
          <div style={{ marginTop: ".5rem" }}>
              {shown.map((r) => (
                <div key={`${r.kind}|${r.message}`} className="hist-line"
                     style={r.dismissed ? { opacity: .6 } : undefined}>
                  {/* the server's words for the line; `message` stays
                      the key dismiss and restore send back */}
                  <div>{r.text}</div>
                  <div className="meta">
                    {/* the subject's whole span: the first day it was
                        ever seen to the last — its age is most of its
                        severity */}
                    {mmdd(r.first_seen)} → {r.active ? "today" : mmdd(r.last_seen)}
                    {" · "}
                    {/* every line keeps its subject's chip, resolved or
                        not; the state rides beside it */}
                    <span className={"pill " + (r.active
                      ? (SEV_PILL[r.severity] ?? "m") : "m")}>
                      {r.kind_label ?? r.kind}</span>
                    {!r.active && <>{" "}<span className="pill g">resolved</span></>}
                    {/* where the subject is acted on (the server's link) */}
                    {r.link && <>{" · "}<Link to={r.link}>open</Link></>}
                    {r.active && r.dismissed ? (
                      <>
                        {" "}<span className="pill m">dismissed</span>{" "}
                        {mayEdit && (
                        <button disabled={busy(r)}
                                onClick={() => act.mutate({ row: r, restore: true })}>
                          restore</button>
                        )}
                      </>
                    ) : r.active ? <>{" "}<span className="pill a">active</span></> : null}
                  </div>
                </div>
              ))}
          </div>
        )}
      </div>
    </>
  );
}
