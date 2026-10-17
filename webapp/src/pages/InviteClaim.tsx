import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, errText } from "../api/client";

/** public claim page for a one-time family invite link
 *  (/app/invite?token=…). Renders pre-auth, like Login. */
export default function InviteClaim({ onDone }: { onDone: () => void }) {
  const token = new URLSearchParams(location.search).get("token") ?? "";
  const peek = useQuery({ queryKey: ["invite-peek", token],
                          queryFn: () => api.invitePeek(token),
                          retry: false, enabled: !!token });
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  // An invite that names an address may be claimed by that address alone —
  // on hosted the link is mailed there and nowhere else, which is the only
  // mailbox proof this pre-auth form has. Show the address and lock the
  // field so the rule the server enforces is the one the page presents.
  const bound = peek.data?.email ?? "";
  const claim = useMutation({
    mutationFn: () => api.inviteClaim(token, bound || email, password),
    onSuccess: onDone,
  });
  const box = (children: React.ReactNode) => (
    <div className="centered"><div className="card"
      style={{ maxWidth: "24rem", width: "100%" }}>{children}</div></div>
  );
  // Only the server's 404 says the link itself is dead. Anything else (a
  // dropped connection, a 502 while the instance restarts) says nothing
  // about the invite, and sending the invitee back to the owner for a
  // fresh link they do not need is the wrong advice.
  const peekDead = peek.isError
    && /^404:/.test(peek.error instanceof Error ? peek.error.message : "");
  if (peek.isError && !peekDead)
    return box(<>
      <h1>Couldn't check your invite</h1>
      <p className="mut">The server didn't answer just now. Your link may
        still be good — try again in a moment.</p>
      <button className="pri" disabled={peek.isFetching}
              onClick={() => peek.refetch()}>
        {peek.isFetching ? "checking…" : "Try again"}</button>
    </>);
  // any error still standing here is the 404
  if (!token || peek.isError)
    return box(<>
      <h1>Invite not valid</h1>
      <p className="mut">This invite link is invalid, already used, or
        expired. Ask the instance owner for a fresh one.</p>
    </>);
  if (peek.isPending)
    return box(<span className="mut">checking your invite…</span>);
  const field = (label: string, type: string, v: string,
                 set: (s: string) => void, extra?: object) => (
    <p><label>{label}<br />
      <input type={type} value={v} required
        onChange={(e) => set(e.target.value)} {...extra}
        style={{ width: "100%", padding: ".5rem", background: "var(--hover)",
                 border: "1px solid var(--line)", borderRadius: "var(--rs)",
                 color: "var(--ink)" }} /></label></p>
  );
  return box(<>
    <h1>Join this Oikonome</h1>
    <p className="mut">You've been invited
      {peek.data.label && !peek.data.label.includes("@")
        ? <> as <b>{peek.data.label}</b></> : null} with{" "}
      <b>{peek.data.role === "viewer" ? "view-only" : peek.data.role}</b>{" "}
      access — {peek.data.role === "viewer"
        ? "you'll see the household's finances but can't change anything"
        : "you can view and edit the household's money — transactions, "
          + "bills and budgets; bank connections, exports, members and "
          + "the account itself stay with the owner"}. Create your account:</p>
    <form onSubmit={(e) => { e.preventDefault(); claim.mutate(); }}>
      {bound
        ? field("Email", "email", bound, () => {}, { readOnly: true })
        : field("Email", "email", email, setEmail)}
      {field("Password (10+ characters)", "password", password, setPassword,
             { minLength: 10 })}
      {claim.isError && (
        <p style={{ color: "var(--red)" }}>{errText(claim.error)}</p>
      )}
      <button className="pri" disabled={claim.isPending}>
        {claim.isPending ? "creating…" : "Create account & sign in"}
      </button>
    </form>
  </>);
}
