import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type ScriptHeartbeat } from "../api/client";
import { patchList } from "../api/cache";
import { isOwner } from "../role";

function age(hours: number): string {
  if (hours < 1) return `${Math.round(hours * 60)}m ago`;
  if (hours < 48) return `${hours.toFixed(1)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

function ScriptRow({ s, viewer }: { s: ScriptHeartbeat; viewer: boolean }) {
  const qc = useQueryClient();
  type Scripts = { scripts: ScriptHeartbeat[] };
  const save = useMutation({
    mutationFn: (body: { expected_hours?: number; alerts?: boolean }) =>
      api.doctorScriptUpdate(s.source, body),
    // the checkbox is bound to the cached row, so it would snap back
    // until the refetch landed — write the new value there first, and
    // restore the old one only if the server refuses
    onMutate: async (body) => {
      await qc.cancelQueries({ queryKey: ["doctor-scripts"] });
      const prev = qc.getQueryData<Scripts>(["doctor-scripts"]);
      patchList<Scripts, ScriptHeartbeat>(qc, ["doctor-scripts"], "scripts",
        (r) => r.source === s.source,
        (r) => ({ ...r,
          ...(body.alerts !== undefined ? { alerts: body.alerts ? 1 : 0 } : {}),
          ...(body.expected_hours !== undefined
              ? { expected_hours: body.expected_hours } : {}) }));
      return { prev };
    },
    onError: (_e, _v, ctx) => {
      if (ctx?.prev) qc.setQueryData(["doctor-scripts"], ctx.prev);
    },
    // only the scripts view can have moved (status is derived from the
    // expectation); the full health run polls on its own every 10s
    onSuccess: () => qc.invalidateQueries({ queryKey: ["doctor-scripts"] }),
  });
  return (
    <tr>
      <td style={{ whiteSpace: "nowrap" }}>
        <span className={"pill " + (s.status === "fresh" ? "g"
          : s.status === "stale" ? "a" : "")}>
          {s.status}
        </span>
      </td>
      <td>
        {s.label ?? s.source}
        {s.label && s.label !== s.source &&
          <span className="mut"> ({s.source})</span>}
      </td>
      <td className="mut" style={{ whiteSpace: "nowrap" }}>
        {age(s.age_hours)}{s.last_rows != null && ` · ${s.last_rows} rows`}
      </td>
      <td style={{ whiteSpace: "nowrap" }}>
        every{" "}
        <input type="number" min={0} max={2160} style={{ width: "4.5em" }}
          defaultValue={s.expected_hours ?? 0}
          disabled={save.isPending || viewer}
          onBlur={(e) => {
            const h = Math.max(0, Math.floor(Number(e.target.value) || 0));
            if (h !== (s.expected_hours ?? 0))
              save.mutate({ expected_hours: h });
          }} />{" "}
        <span className="mut">h (0 = don't warn)</span>
      </td>
      <td style={{ whiteSpace: "nowrap" }}>
        <label className="mut">
          <input type="checkbox" checked={!!s.alerts}
            disabled={save.isPending || viewer}
            onChange={(e) => save.mutate({ alerts: e.target.checked })} />
          {" "}email me when stale
        </label>
      </td>
    </tr>
  );
}

export default function Doctor({ embedded = false }:
                                    { embedded?: boolean } = {}) {
  // the page keeps itself current, like the wizard's sync step —
  // no manual re-run needed (the button stays for impatience)
  // the collector watch settings write through a script-token door, which
  // is the owner's — members and viewers alike see them disabled-in-place
  // (the values are still informative)
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  // Hosted never runs the checks: the page says so below and the queries
  // do not fire, let alone poll. The polls themselves only run while this
  // component is mounted and the tab visible (react-query stops the
  // interval for a hidden tab) — Settings mounts it only while its System
  // section is open for exactly that reason.
  const relevant = !!me.data && !me.data.hosted;
  const q = useQuery({ queryKey: ["doctor"], queryFn: api.doctor,
                       enabled: relevant, refetchInterval: 10_000 });
  const scripts = useQuery({
    queryKey: ["doctor-scripts"], queryFn: api.doctorScripts,
    enabled: relevant, refetchInterval: 30_000 });
  const viewer = !isOwner(me);
  // Hosted: instance health is the operator's job — hide entirely
  if (me.data?.hosted)
    return embedded ? null : (
      <div className="card">System health is managed by the host on this
        instance.</div>);

  if (q.isPending)
    return <div className="centered"><span className="mut">running checks…</span></div>;
  if (q.isError)
    return <div className="card">Couldn't run the checks: {String(q.error)}</div>;

  const d = q.data;
  const sections: { title: string; rows: typeof d.checks }[] = [];
  for (const c of d.checks) {
    const s = sections.find((x) => x.title === (c.section || "Checks"));
    if (s) s.rows.push(c);
    else sections.push({ title: c.section || "Checks", rows: [c] });
  }
  const updated = q.dataUpdatedAt
    ? new Date(q.dataUpdatedAt).toLocaleTimeString() : null;
  return (
    <>
      <div style={{ display: "flex", alignItems: "baseline", gap: "1rem",
                    flexWrap: "wrap" }}>
        {!embedded &&
          <h1 style={{ marginBottom: 0 }}>System health</h1>}
        <span className="mut" style={{ fontSize: 13 }}>
          {d.ok ? "all checks pass"
            : `${d.checks.filter((c) => !c.ok).length} check${
                d.checks.filter((c) => !c.ok).length === 1 ? "" : "s"
              } need${d.checks.filter((c) => !c.ok).length === 1
                ? "s" : ""} attention`}
          {updated && <> · auto-refreshes — checked {updated}</>}
          {q.isFetching && " ⟳"}
        </span>
      </div>

      {sections.map((s) => (
        <div className="card" key={s.title}>
          <h2 style={{ marginTop: 0 }}>{s.title}</h2>
          <table>
            <tbody>
              {s.rows.map((c) => (
                <tr key={c.name + c.detail.slice(0, 8)}>
                  <td style={{ whiteSpace: "nowrap", width: "1%" }}>
                    <span className={"pill " + (c.ok ? "g" : c.severity === "warn" ? "a" : "r")}>
                      {c.ok ? "ok" : c.severity === "warn" ? "warn" : "fail"}
                    </span>
                  </td>
                  <td style={{ whiteSpace: "nowrap" }}>{c.name}</td>
                  <td className="mut">{c.detail}
                    {/* live LLM work — determinate when the job
                        reports counts, indeterminate while busy */}
                    {c.progress != null ? (
                      <div className="rcpt-progress"
                           style={{ marginTop: ".3rem", maxWidth: "22rem" }}>
                        <span style={{ animation: "none", left: 0,
                                       width: `${c.progress}%` }} />
                      </div>
                    ) : c.busy ? (
                      <div className="rcpt-progress"
                           style={{ marginTop: ".3rem", maxWidth: "22rem" }}>
                        <span /></div>
                    ) : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}

      {(scripts.data?.scripts.length ?? 0) > 0 && (
        <div className="card">
          <h2>Data collectors</h2>
          <p className="mut">Every script or import that pushed data into
            this instance, freshest first. A watched source goes stale after
            missing two expected intervals.</p>
          <table>
            <tbody>
              {scripts.data!.scripts.map((s) => (
                <ScriptRow key={s.source} s={s} viewer={viewer} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      <div className="card">
        <div style={{ display: "flex", gap: "1rem", alignItems: "center" }}>
          <button disabled={q.isFetching} onClick={() => q.refetch()}>
            {q.isFetching ? "checking…" : "Re-run now"}
          </button>
          <a href="/api/doctor/bundle" download="oikonome-system-health.json">
            Download the diagnostic bundle
          </a>
        </div>
        <p className="mut" style={{ marginBottom: 0 }}>The bundle is what bug
          reports ask for — check output and versions only, no tokens and no
          transaction contents.</p>
      </div>

    </>
  );
}
