import { useMutation, useQueryClient } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, errText, mmdd, type Me } from "../api/client";

/** Whether this account still has an emailed confirmation link to act on.
 *  Hosted only: there the household's mail goes to verified owners alone,
 *  so until the link is opened nothing the schedule promises arrives. */
export function awaitsEmailConfirm(me: Me | undefined): boolean {
  return !!me?.hosted && me.verified === false;
}

/** Whether the add-a-bank doors are closed until an owner confirms their
 *  address. The server says so (`features.bank_awaits_confirm`), because
 *  only it knows why the allowance is 0: an ordinary free trial waits on
 *  confirmation, a paying household does not, whether or not its owner
 *  has confirmed. Only a server too old to say falls back to "hosted and
 *  unconfirmed". (mobile: bankAwaitsConfirm in lib/pure) */
export function bankAwaitsConfirm(me: Me | undefined): boolean {
  const said = me?.features?.bank_awaits_confirm;
  return typeof said === "boolean" ? said : awaitsEmailConfirm(me);
}

/** Why the hosted bank door is closed to an unconfirmed address: until
 *  an owner of the household has confirmed, the plan's institution
 *  allowance is zero and the server refuses the link. Said on the card
 *  above the door and as the closed button's explanation, so the refusal
 *  is read before the tap rather than after it. Statement and CSV imports
 *  stay open. (mobile: CONFIRM_TO_CONNECT in verify-email-card) */
export const CONFIRM_TO_CONNECT =
  "Confirm your email to connect a bank — the link is in your inbox.";

// The days a new account is left alone about its unconfirmed address:
// the top banner waits them out, and so does the sentence about freezing.
// A person who signed up a minute ago and is looking at "check your inbox"
// has not ignored anything yet.
const VERIFY_GRACE_MS = 3 * 24 * 60 * 60 * 1000;
const VERIFY_NEAR_MS = 7 * 24 * 60 * 60 * 1000;
/** True once the account is past the grace days (or its age is unknown —
 *  fail toward saying it), and always once the freeze is a week away:
 *  the deadline is the HOUSEHOLD's, counted from its oldest signup, so a
 *  member invited yesterday can be days from a freeze they must be told
 *  about. */
export function verifyNagDue(createdAt: string | null | undefined,
                             deadline?: string | null): boolean {
  if (deadline) {
    const d = Date.parse(deadline);
    if (!Number.isNaN(d) && d - Date.now() <= VERIFY_NEAR_MS) return true;
  }
  if (!createdAt) return true;
  const t = Date.parse(createdAt);
  if (Number.isNaN(t)) return true;
  return Date.now() - t >= VERIFY_GRACE_MS;
}

/** The way to ask for the confirmation mail again, at any account age.
 *  The top banner waits out a grace period so a brand-new signup is not
 *  nagged on day one; this card does not, because a link that went to
 *  spam, expired, or was spent by a mail scanner leaves no other way to
 *  get a fresh one — and the error page for a bad link sends people here.
 *  (mobile: Security & household, the same card) */
export function VerifyEmailCard({ me, inline, children }: {
  me: Me | undefined;
  // inside a step that is already a card (the setup wizard): a paragraph,
  // not a card of its own
  inline?: boolean;
  // what waiting on the address holds up, in the place the card sits
  children?: ReactNode;
}) {
  const qc = useQueryClient();
  const resend = useMutation({
    mutationFn: () => api.verifyResend(),
    // already confirmed (in another tab, or the link was opened since):
    // the refreshed /api/me takes this card away
    onSuccess: (r) => { if (r.verified) qc.invalidateQueries({ queryKey: ["me"] }); },
  });
  if (!me || !awaitsEmailConfirm(me)) return null;
  const body = (
    <>
      <p className="mut" style={{ marginTop: 0 }}>
        {children && <>{children} </>}
        We sent a link to <b>{me.email}</b>. No email? Check spam, or
        resend.
        {me.verify_deadline
          && verifyNagDue(me.created_at, me.verify_deadline) && <>
          {" "}Unconfirmed accounts are frozen on{" "}
          {mmdd(me.verify_deadline)} and deleted a week later.</>}
      </p>
      <div style={{ display: "flex", gap: ".6rem", alignItems: "center",
                    flexWrap: "wrap", marginBottom: inline ? ".6rem" : 0 }}>
        <button disabled={resend.isPending} onClick={() => resend.mutate()}>
          {resend.isPending ? "sending…" : "Resend"}
        </button>
        {resend.isSuccess && (
          <span className="mut">{resend.data.verified
            ? "Already confirmed." : "Confirmation email sent."}</span>)}
        {resend.isError && (
          <span className="neg">Resend failed: {errText(resend.error)}</span>)}
      </div>
    </>
  );
  if (inline) return body;
  return (
    <div className="card">
      <h2>Confirm your email</h2>
      {body}
    </div>
  );
}
