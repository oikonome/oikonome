import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, errText } from "../api/client";
import Note from "./Note";

// The two ways out of an account, shared by Settings and by every screen
// that stands in front of the app (the second-factor gate, an add-on's
// lockout). The server keeps both doors open behind those screens, so the
// screens have to offer them: a person who wants out must not be told to
// enrol a factor or pay first. One copy, so the typed confirmation and the
// wording cannot drift between the places that offer it.

/** A household member deletes their OWN login — the owner's account and
 *  every household row stay. Same re-auth as the
 *  owner's erasure door; lands on the sign-in page afterwards. */
export function LeaveHouseholdCard() {
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const leave = useMutation({
    // elevation-gated; the typed phrase is the are-you-sure
    mutationFn: () => api.accountLeave(),
    onSuccess: () => { window.location.href = "/login"; },
    onError: (e) => setErr(errText(e)),
  });
  return (
    <div className="card" style={{ borderColor: "rgba(224,105,93,.45)" }}>
      <h2 style={{ color: "var(--red)" }}>Delete my login</h2>
      <p className="mut">Removes <b>your</b> login from this household and
        signs you out. The household's data and the owner's account are not
        affected — the owner can invite you again any time.</p>
      {err && <Note tone="bad" onDismiss={() => setErr(null)}>{err}</Note>}
      <div className="field-grid" style={{ maxWidth: "26rem" }}>
        <label>Type <b>delete my login</b> to confirm
          <input value={confirm} placeholder="delete my login"
                 onChange={(e) => setConfirm(e.target.value)} /></label>
      </div>
      <button className="pri" disabled={leave.isPending
                || confirm !== "delete my login"}
              style={{ marginTop: ".8rem", background: "var(--danger)" }}
              onClick={() => leave.mutate()}>
        {leave.isPending ? "deleting…" : "Delete my login"}
      </button>
    </div>
  );
}

// Delete-account GUI over the existing /api/account/delete (full
// tenant erasure, FRESH elevation — the sheet asks again even inside an
// open window — rate-limited). Hosted CCPA duty and self-host courtesy
// alike; owner-only (viewers leave from Settings → Household, they don't
// erase it). `exportHint` says where the copy is taken from on the screen
// showing the card — Settings has a Data section, a lockout does not.
export function DeleteAccountCard({ owner, exportHint }: {
  owner: boolean; exportHint?: string;
}) {
  const [confirm, setConfirm] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const del = useMutation({
    // the typed phrase is the are-you-sure; identity is the sheet's job
    mutationFn: () => api.accountDelete(),
    // land on the dedicated confirmation page, not a bare login form
    onSuccess: () => { window.location.href = "/app/account-deleted"; },
    onError: (e) => {
      const m = String(e instanceof Error ? e.message : e);
      setErr(m.includes("too many requests") ? "Too many attempts - try later."
        : errText(e));
    },
  });
  if (!owner) return null;
  return (
    <div className="card" style={{ borderColor: "rgba(224,105,93,.45)" }}>
      <h2 style={{ color: "var(--red)" }}>Delete account</h2>
      <p className="mut">Erases this household permanently: every account,
        transaction, budget, and setting — and signs everyone out. There is
        no undo. {exportHint
          ?? "Export your data first (Data section) if you want a copy."}</p>
      {err && <Note tone="bad" onDismiss={() => setErr(null)}>{err}</Note>}
      <div className="field-grid" style={{ maxWidth: "26rem" }}>
        <label>Type <b>delete everything</b> to confirm
          <input value={confirm} placeholder="delete everything"
                 onChange={(e) => setConfirm(e.target.value)} /></label>
      </div>
      <button className="pri" disabled={del.isPending
                || confirm !== "delete everything"}
              style={{ marginTop: ".8rem", background: "var(--danger)" }}
              onClick={() => del.mutate()}>
        {del.isPending ? "deleting…" : "Delete my account forever"}
      </button>
    </div>
  );
}

/** The way out on a screen that stands in front of the app: a quiet link
 *  that opens the card the caller's role is served. The owner erases the
 *  household (the leave door refuses an owner — it would orphan it);
 *  anyone else removes their own login (the erasure door is the owner's).
 *  A role not known yet offers neither, so no one is handed a door the
 *  server refuses on every press. */
export function AccountExit({ role, exportHint }: {
  role: string | null | undefined; exportHint?: string;
}) {
  const [open, setOpen] = useState(false);
  const owner = role === "owner";
  const other = !!role && role !== "owner";
  if (!owner && !other) return null;
  return (
    <div style={{ marginTop: ".8rem" }}>
      <p className="mut" style={{ fontSize: 13, margin: 0 }}>
        <button type="button" className="as-link" aria-expanded={open}
           onClick={(e) => { e.preventDefault(); setOpen((o) => !o); }}>
          {owner ? "Delete account" : "Leave household"}</button>
      </p>
      {open && (
        <div style={{ marginTop: ".6rem" }}>
          {owner ? <DeleteAccountCard owner exportHint={exportHint} />
                 : <LeaveHouseholdCard />}
        </div>
      )}
    </div>
  );
}
