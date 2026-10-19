import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errText, kindIs, type Account } from "../api/client";

// "Which checking account does the cash forecast anchor on?" — asked in
// the wizard's Connect step once the banks are in. One eligible checking
// account is pinned by the server when the step is left, so the control
// only says so; two or more are a choice made here, on the tap (the
// Accounts page's edit row keeps the same switch for later). The Today
// checklist shows the same pills under its step (`compact`: the step's
// own title and text stand in for the heading). Mobile:
// components/wizard-primary-checking.tsx.
export default function PrimaryCheckingPick({ candidates, pinned, compact }: {
  // the eligible account ids, from /api/onboarding (checking_candidates)
  candidates: string[];
  pinned: string | null;
  compact?: boolean;
}) {
  const qc = useQueryClient();
  const accts = useQuery({ queryKey: ["accounts"], queryFn: api.accounts,
                           enabled: candidates.length > 0 });
  const pick = useMutation({
    mutationFn: (a: Account) => api.accountClassify(a.id, a.kind, true),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["onboarding"] });
      qc.invalidateQueries({ queryKey: ["settings"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
  });
  if (!candidates.length) return null;
  const rows = (accts.data?.accounts ?? [])
    .filter((a) => candidates.includes(a.id) && kindIs(a, "depository/checking"));
  const name = (a: Account) =>
    `${a.institution_name ? `${a.institution_name} · ` : ""}${a.name}`
    + (a.mask ? ` ····${a.mask}` : "");
  // one candidate already pinned: nothing to choose. One candidate and no
  // live pin (a dead pin kept for its bank's reconnect) is still a tap —
  // the text branch would leave the step open with no way to close it
  if (candidates.length === 1 && pinned === candidates[0]) {
    const only = rows[0];
    return (
      <div>
        {!compact && <b>Primary checking</b>}
        <p className="mut" style={{ margin: ".2rem 0 0", fontSize: 13 }}>
          {only ? <><b style={{ color: "var(--ink)" }}>{name(only)}</b> — </>
                : null}
          the cash forecast anchors on your one checking account. Change
          it any time from Accounts.</p>
      </div>
    );
  }
  return (
    <div>
      {!compact && <>
        <b>Primary checking</b>
        <p className="mut" style={{ margin: ".2rem 0 .4rem", fontSize: 13 }}>
          Which checking account does the cash forecast anchor on? Runway,
          low point and the timeline all read from this one.</p>
      </>}
      <div className="wiz-pills" role="radiogroup"
           aria-label="Primary checking account">
        {rows.map((a) => (
          <button key={a.id} type="button" role="radio"
                  aria-checked={a.id === pinned}
                  className={"wiz-pill" + (a.id === pinned ? " sel" : "")}
                  disabled={pick.isPending}
                  onClick={() => { if (a.id !== pinned) pick.mutate(a); }}>
            {name(a)}</button>
        ))}
      </div>
      {pick.isError && (
        <p className="neg" style={{ fontSize: 13, margin: ".3rem 0 0" }}>
          {errText(pick.error)}</p>)}
    </div>
  );
}
