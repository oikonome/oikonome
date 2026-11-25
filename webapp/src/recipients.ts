// The daily-mail recipient list's client-side rules, kept free of React so
// the mobile pure-logic suite runs the same cases against this copy and the
// phone's (mobile/src/lib/pure.ts): both add boxes refuse the same input
// with the same words, and both say the same thing after Resend.

/** Is this shaped like one mailbox? Never stricter than the server, which
 *  accepts a dotless domain on purpose: a LAN relay's single-label domain
 *  is a real mailbox. The server has the final say. */
export function looksLikeMailbox(v: string): boolean {
  return /^[^\s@]+@[^\s@.]+(\.[^\s@.]+)*$/.test(v);
}

/** Why the add box will not add this address, which is already on the
 *  list, or null when it is new. The owner is mailed by construction and
 *  never invited to their own household's email. */
export function recipientDuplicate(
  v: string, recipients: string[], ownerEmail: string | null | undefined,
): string | null {
  const low = v.toLowerCase();
  if (ownerEmail && ownerEmail.toLowerCase() === low)
    return "That's you — you're already on the list.";
  if (recipients.some((r) => r.toLowerCase() === low))
    return "Already on the list.";
  return null;
}

/** What a member's own daily-email card says about their inbox. The
 *  worker's rule is not "on and not muted": once the owner saves a list,
 *  only the owner and the addresses that ACCEPTED their invitation are
 *  mailed; with no list, every verified user is (on self-host, everyone,
 *  until anyone has verified). So the sentence is worked out from the
 *  same facts. `recipients`/`status` come from the settings view and are
 *  undefined until it loads; `needBaseUrl` from /api/notify, absent on an
 *  older server. A member's view carries only their own entry of the
 *  roster, so it also carries `listConfigured` (a list is set) and
 *  `onList` (that list, split as the worker splits it, names them); when
 *  present they decide, and an owner's view or an older server falls back
 *  to the roster. A member on the list gets one status row for their own
 *  mailbox however the entry was stored: `mailed` is the mailer's answer
 *  and `not_mailed` why it says no (`held`, or the invitation's state). */
export function myDailyLine(x: {
  me: string; on: boolean; muted: boolean;
  prefs?: Partial<Record<string, boolean>> | null;
  verified?: boolean | null; hosted?: boolean;
  recipients?: string[] | null;
  listConfigured?: boolean | null; onList?: boolean | null;
  status?: { email: string; verified?: boolean | null;
             invite?: string | null; delivery?: string | null;
             mailed?: boolean | null; not_mailed?: string | null }[]
           | null;
  needBaseUrl?: boolean;
}): { receiving: boolean; text: string } {
  const no = (text: string) => ({ receiving: false, text });
  if (x.muted) return no("You've muted all household mail for yourself.");
  if (!x.on) return no("Off.");
  if (x.prefs?.daily === false) return no("You've turned the daily email off for yourself.");
  const low = x.me.toLowerCase();
  const row = x.status?.find((r) => r.email.toLowerCase() === low);
  if (row?.delivery === "bouncing" || row?.delivery === "complained")
    return no("Held — mail to your address isn't being delivered.");
  const listed = x.listConfigured != null ? x.listConfigured
    : !!x.recipients?.length;
  if (x.status && listed) {
    if (x.onList === false || !row)
      return no("The owner's mail list doesn't include you.");
    // a member's own row carries the mailer's verdict and, when it does
    // not mail them, why; an older server's row has only the invitation
    const state = row.mailed == null ? row.invite
      : row.mailed ? "accepted" : row.not_mailed ?? row.invite;
    if (state === "held")
      return no("Held until the owner confirms the mail list.");
    if (state !== "accepted") {
      if (x.needBaseUrl)
        return no("Your invitation can't be sent until the server has a "
          + "public address (OIKONOME_BASE_URL).");
      if (state === "invited")
        return no("Accept the invitation in your inbox to start receiving it.");
      if (state === "declined")
        return no("You declined the invitation — ask the owner to send another.");
      return no("You haven't accepted an invitation yet — ask the owner to "
        + "send one.");
    }
  } else if (x.verified === false
             && (x.hosted || x.status?.some((r) => r.verified === true))) {
    return no("Confirm your email address to receive it.");
  }
  return { receiving: true, text: "You receive it." };
}

/** What the add box says instead of adding, when this server cannot mail
 *  the invitation an added address needs before it is ever mailed. */
export const NEED_BASE_URL =
  "Set OIKONOME_BASE_URL on the server before adding recipients.";

/** Why Resend sent nothing, or null when it sent. The server answers ok
 *  even when it held the mail back, and names why in `reason`; a reply
 *  without `sent` comes from a server that always sent. */
export function resendHeld(
  r: { sent?: boolean | null; reason?: string | null } | null | undefined,
  email: string,
): string | null {
  if (r?.sent !== false) return null;
  if (r.reason === "cooldown")
    return `An invitation went to ${email} a few minutes ago — try again `
      + "shortly.";
  if (r.reason === "cap")
    return "Too many invitations sent for now — try again later.";
  if (r.reason === "no_mail")
    return `The invitation to ${email} was not sent: this instance has no `
      + "way to mail it (no relay, or no OIKONOME_BASE_URL to build the "
      + "link under). Their previous link, if any, still works.";
  if (r.reason === "undelivered")
    return `The mail relay refused the invitation to ${email}. Their `
      + "previous link, if any, still works.";
  if (r.reason === "error")
    return `The invitation to ${email} could not be sent right now — try `
      + "again shortly.";
  return `The invitation to ${email} was not sent.`;
}

// ---- who receives which kind of email ----
// The server's kinds (notify/email_prefs.py), in the grid's column order.
export const EMAIL_KINDS = ["daily", "monthly", "yearly", "alerts"] as const;
export type EmailKind = (typeof EMAIL_KINDS)[number];
export const EMAIL_KIND_LABEL: Record<EmailKind, string> = {
  daily: "Daily", monthly: "Monthly", yearly: "Yearly", alerts: "Alerts",
};

/** Whether this address receives this kind; a row from a server that
 *  predates the grid carries no prefs, and that means all of them. */
export function prefOn(
  prefs: Partial<Record<string, boolean>> | null | undefined, kind: string,
): boolean {
  return prefs?.[kind] !== false;
}

/** Why a column is dimmed: the household does not send that kind at all
 *  right now, so the boxes under it decide nothing until it is on.
 *  Alerts have no schedule and are never dimmed. */
export function kindOffNote(
  kind: string, sending: Partial<Record<string, boolean>> | null | undefined,
): string | null {
  if (kind === "alerts" || sending?.[kind]) return null;
  return kind === "daily" ? "daily email is off"
    : kind === "yearly" ? "year in review is off"
    : `${kind} report is off`;
}

/** The recipient rows with one box flipped — the cache patch a tap makes
 *  before the server's reply settles it. Matches the address the way the
 *  server does, ignoring case. */
export function withPref<T extends { email: string;
                                     prefs?: Partial<Record<string, boolean>> }>(
  rows: T[], email: string, kind: string, on: boolean,
): T[] {
  const low = email.toLowerCase();
  return rows.map((r) => r.email.toLowerCase() === low
    ? { ...r, prefs: { ...r.prefs, [kind]: on } } : r);
}

/** The facts myDailyLine works from, as /api/me and /api/settings hand
 *  them to this person: the one place the Daily email card and the
 *  Settings list's subtitle read them, so the two can never disagree about
 *  whether the email reaches them. null until /api/me says (an older
 *  server sends no daily_email). */
export function myDailyFrom(
  me: { email?: string | null; verified?: boolean | null; hosted?: boolean;
        daily_email?: { on: boolean; hour: number; muted: boolean;
                        prefs?: Partial<Record<string, boolean>> } | null }
      | null | undefined,
  settings: { email_recipients?: string[] | null;
              email_list_configured?: boolean | null;
              email_on_list?: boolean | null;
              email_recipient_status?: NonNullable<
                Parameters<typeof myDailyLine>[0]["status"]> | null }
      | null | undefined,
  needBaseUrl?: boolean,
): { receiving: boolean; text: string } | null {
  const d = me?.daily_email;
  if (!d) return null;
  return myDailyLine({
    me: me.email ?? "", on: d.on, muted: d.muted, prefs: d.prefs,
    verified: me.verified, hosted: me.hosted,
    recipients: settings?.email_recipients,
    listConfigured: settings?.email_list_configured,
    onList: settings?.email_on_list,
    status: settings?.email_recipient_status,
    needBaseUrl });
}

/** The Settings list's email subtitle for someone who is not the owner:
 *  their OWN daily email, read off /api/me's daily_email and the verdict
 *  the Daily email card shows (myDailyFrom) — whether the household sends
 *  it, whether it reaches them, and only then the hour. Never a default:
 *  the household's schedule is the owner's to read, and a "daily 7am" to
 *  someone the email does not reach (left off the owner's list, an
 *  invitation not accepted, an address not confirmed) would be a wrong
 *  figure. null while /api/me has not said (an older server), so the list
 *  shows no subtitle at all. */
export function myEmailSubtitle(
  me: Parameters<typeof myDailyFrom>[0],
  settings: Parameters<typeof myDailyFrom>[1],
): string | null {
  const d = me?.daily_email;
  if (!d) return null;
  if (!d.on) return "daily email off";
  if (d.muted || d.prefs?.daily === false) return "daily email off for you";
  if (!myDailyFrom(me, settings)?.receiving) return "not receiving it";
  const h = d.hour;
  return `daily ${((h + 11) % 12) + 1}${h < 12 ? "am" : "pm"}`;
}
