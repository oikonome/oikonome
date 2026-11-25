import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { api } from "../api/client";
import { FailedLine } from "../components/Loaded";
import { failLine } from "../moneymath";
import SegmentedControl from "../components/SegmentedControl";
import Note from "../components/Note";

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
        <Note style={{ marginBottom: ".8rem",
              background: "rgba(92,181,107,.10)",
              borderColor: "rgba(92,181,107,.4)" }}>
          {send.data.delivery === "emailed"
            ? <>Sent — thank you! Your {kind === "bug" ? "bug report" : "feedback"}
                number is <b>{send.data.number}</b>; quote it in any follow-up.</>
            : <>Packaged as <b>{send.data.number}</b> — the zip just downloaded.
                Email it to <b>{feedbackTo}</b>.</>}
        </Note>
      )}
      {send.isError && (
        <Note style={{ marginBottom: ".8rem",
              background: "rgba(224,105,93,.10)",
              borderColor: "rgba(224,105,93,.4)", color: "var(--red)" }}>
          {failLine(send.error, "your report", "web", "send").text}</Note>
      )}
      <SegmentedControl label="Kind of report" value={kind} onChange={setKind}
        style={{ margin: ".2rem 0 .6rem" }}
        options={[{ value: "feedback", label: "Feedback" },
                  { value: "bug", label: "Report a bug" }]} />
      {/* One line: what to write, and where it goes. The package is the
          system-health bundle plus recent log lines — never transaction
          contents, tokens, hostnames or bank names — and the line says the
          part a person weighs before pressing Send. */}
      <p className="mut" style={{ margin: ".4rem 0 .7rem" }}>
        {kind === "bug"
          ? "Say what happened, what you expected, and where"
          : "Something confusing, missing, or worth changing?"}
        {" — "}
        {canEmail
          ? <>it goes to {feedbackTo ? <b>{feedbackTo}</b> : "the maintainer"}
              {" "}with a diagnostic bundle (no bank names or transactions).</>
          : <>this instance has no email, so it downloads as a .zip to send
              to {feedbackTo ? <b>{feedbackTo}</b> : "the maintainer"}.</>}
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
        <input aria-label="Attach a screenshot" type="file" accept=".png,.jpg,.jpeg,.gif,.webp"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        <button className="pri" style={{ marginLeft: "auto" }}
          disabled={send.isPending || !text.trim()}
          onClick={() => send.mutate()}>
          {send.isPending ? "packaging…"
            : canEmail ? (feedbackTo ? `Send to ${feedbackTo}` : "Send")
              : "Build my report .zip"}
        </button>
      </div>
    </div>
  );
}

export default function Feedback() {
  const q = useQuery({ queryKey: ["testing"], queryFn: api.testing });
  if (q.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;
  if (q.isLoadingError)
    return <div className="card"><FailedLine q={q} what="the feedback page" /></div>;
  const d = q.data;
  if (!d.enabled)
    return (
      <>
        <h1>Feedback &amp; bug reports</h1>
        <div className="card">
          <p style={{ margin: 0 }}>Feedback and bug reports are <b>turned
            off</b> on this instance. The owner can turn them back on under{" "}
            <Link to="/settings/data">Settings › Data</Link>.</p>
        </div>
      </>
    );
  return (
    <>
      <h1>Feedback &amp; bug reports</h1>
      <FeedbackCard canEmail={d.can_email} feedbackTo={d.feedback_to} />
    </>
  );
}
