import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, type TestingState } from "../api/client";
import { patchQuery, saveSettings } from "../api/cache";

/** The feedback pipeline: message + optional screenshot, emailed
 *  to the maintainer (or handed back as a zip when SMTP isn't set up).
 *  One form, two kinds — feedback, or a bug report shaped by three
 *  prompts — so a person who hit a defect recognises this as the place
 *  to say so. */

export type FeedbackKind = "feedback" | "bug";

/** The bug prompts fold into one message with labelled sections; the
 *  server stores and mails a single text either way. Empty sections are
 *  left out so a two-line report is not padded with blank headings. */
export function composeBugReport(happened: string, expected: string,
                                 where: string): string {
  const parts: string[] = [];
  if (happened.trim()) parts.push(`What happened:\n${happened.trim()}`);
  if (expected.trim()) parts.push(`What I expected:\n${expected.trim()}`);
  if (where.trim()) parts.push(`Where:\n${where.trim()}`);
  return parts.join("\n\n");
}

const inputStyle = { width: "100%", padding: ".5rem", background: "var(--hover)",
                     border: "1px solid var(--line)", borderRadius: "var(--rs)",
                     color: "var(--ink)" } as const;

function FeedbackCard({ canEmail, feedbackTo }:
                      { canEmail: boolean; feedbackTo: string }) {
  const [kind, setKind] = useState<FeedbackKind>("feedback");
  const [message, setMessage] = useState("");
  const [happened, setHappened] = useState("");
  const [expected, setExpected] = useState("");
  const [where, setWhere] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const text = kind === "bug"
    ? composeBugReport(happened, expected, where) : message;
  const send = useMutation({
    mutationFn: () => api.testingFeedback(text, file, kind),
    onSuccess: () => { setMessage(""); setHappened(""); setExpected("");
                       setWhere(""); },
  });
  return (
    <div className="card" style={{ marginBottom: "1rem",
                                   borderColor: "rgba(230,177,89,.45)" }}>
      {send.isSuccess && (
        <div className="note" style={{ marginBottom: ".8rem",
              background: "rgba(92,181,107,.10)",
              borderColor: "rgba(92,181,107,.4)" }}>
          {send.data.delivery === "emailed"
            ? <>Sent — thank you! Your {kind === "bug" ? "bug report" : "feedback"}
                number is <b>{send.data.number}</b>; quote it in any follow-up.</>
            : <>Packaged as <b>{send.data.number}</b> — the zip just downloaded.
                Email it to <b>{feedbackTo}</b>.</>}
        </div>
      )}
      {send.isError && (
        <div className="note" style={{ marginBottom: ".8rem",
              background: "rgba(224,105,93,.10)",
              borderColor: "rgba(224,105,93,.4)", color: "var(--red)" }}>
          {String(send.error)}</div>
      )}
      <h2 style={{ marginTop: 0 }}>Feedback &amp; bug reports</h2>
      <div className="seg" role="radiogroup" aria-label="Kind of report"
           style={{ display: "inline-flex", gap: ".3rem", margin: ".2rem 0 .6rem" }}>
        {(["feedback", "bug"] as FeedbackKind[]).map((k) => (
          <button key={k} type="button" role="radio"
                  aria-checked={kind === k}
                  className={kind === k ? "pri" : undefined}
                  onClick={() => setKind(k)}>
            {k === "bug" ? "Report a bug" : "Feedback"}
          </button>
        ))}
      </div>
      <p className="mut" style={{ margin: ".4rem 0 .7rem" }}>
        {kind === "bug"
          ? <>Something broke? Say what happened, what you expected, and
              where — a screenshot helps a lot.</>
          : <>Something confusing, missing, or worth changing? Tell us.</>}{" "}
        Your report is packaged with the system-health diagnostic bundle and the
        app's recent log lines (no transaction contents, no tokens) and gets a
        report number.{" "}
        {canEmail
          ? (feedbackTo
              ? <>It goes straight to <b>{feedbackTo}</b>.</>
              : <>It goes straight to the maintainer.</>)
          : <>This instance has no email configured, so the package downloads
              as a .zip — <b>attach it to an email to {feedbackTo}</b>.</>}
      </p>
      {kind === "bug" ? (
        <>
          <textarea rows={3} value={happened} required
            placeholder="What happened?"
            aria-label="What happened"
            onChange={(e) => setHappened(e.target.value)} style={inputStyle} />
          <textarea rows={2} value={expected}
            placeholder="What did you expect instead?"
            aria-label="What you expected"
            onChange={(e) => setExpected(e.target.value)}
            style={{ ...inputStyle, marginTop: ".5rem" }} />
          <input value={where}
            placeholder="Which page or screen were you on?"
            aria-label="Where it happened"
            onChange={(e) => setWhere(e.target.value)}
            style={{ ...inputStyle, marginTop: ".5rem" }} />
        </>
      ) : (
        <textarea rows={3} value={message} required
          placeholder="what you'd change, what was confusing, what you'd want…"
          aria-label="Your feedback"
          onChange={(e) => setMessage(e.target.value)} style={inputStyle} />
      )}
      <div style={{ display: "flex", gap: ".7rem", alignItems: "center",
                    marginTop: ".5rem", flexWrap: "wrap" }}>
        <input type="file" accept=".png,.jpg,.jpeg,.gif,.webp"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <button className="pri" style={{ marginLeft: "auto" }}
          disabled={send.isPending || !text.trim()}
          onClick={() => send.mutate()}>
          {send.isPending ? "packaging…"
            : canEmail ? `Send to ${feedbackTo}` : "Build my report .zip"}
        </button>
      </div>
    </div>
  );
}

// instance-wide feedback on/off (owner; viewers see the state only)
function FeedbackToggle({ enabled }: { enabled: boolean }) {
  const qc = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: api.me });
  const flip = useMutation({
    mutationFn: () => saveSettings(qc, { feedback_enabled: !enabled }),
    onSuccess: () => {
      // the page flips on ["testing"] — write the one bit it changed now,
      // and let the refetch confirm the rest of that view
      patchQuery<TestingState>(qc, ["testing"], { enabled: !enabled });
      qc.invalidateQueries({ queryKey: ["testing"] });
    },
  });
  if (me.data?.role !== "owner") return null;
  return (
    <p style={{ marginBottom: 0 }}>
      <button disabled={flip.isPending} onClick={() => flip.mutate()}>
        {enabled ? "Turn feedback off for this instance"
          : "Turn feedback back on"}
      </button>
    </p>
  );
}

export default function Feedback() {
  const q = useQuery({ queryKey: ["testing"], queryFn: api.testing });
  if (q.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;
  if (q.isError)
    return <div className="card">Couldn't load: {String(q.error)}</div>;
  const d = q.data;
  if (!d.enabled)
    return (
      <>
        <h1>Feedback &amp; bug reports</h1>
        <div className="card">
          <p style={{ margin: 0 }}>Feedback and bug reports are <b>turned
            off</b> on this instance. The owner can re-enable them here or
            on the Settings page.</p>
          <FeedbackToggle enabled={false} />
        </div>
      </>
    );
  return (
    <>
      <h1>Feedback &amp; bug reports</h1>
      <FeedbackToggle enabled />
      <FeedbackCard canEmail={d.can_email} feedbackTo={d.feedback_to} />
    </>
  );
}
