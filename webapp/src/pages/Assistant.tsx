// local ledger assistant. Ask plain-language questions about your
// own finances; the model routes to read-only tools and answers from what the
// engine computes. On the bundled LLM your data never leaves the box; when
// the assistant backend is a remote host the header says where answers go.
// The whole page hides (via the nav gate) when no LLM is configured.
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Link } from "react-router";
import { assistant, errText } from "../api/client";

interface Turn { q: string; a?: string; err?: string; tools?: string[]; }

const EXAMPLES = [
  "What's my net worth?",
  "Where did my money go last month?",
  "What bills are coming up?",
  // the question people actually have
  "Can I afford $2,000 this month?",
];

export default function Assistant() {
  const status = useQuery({ queryKey: ["assistant-status"], queryFn: assistant.status });
  const [q, setQ] = useState("");
  const [log, setLog] = useState<Turn[]>([]);

  const ask = useMutation({
    mutationFn: (question: string) => assistant.ask(question),
    onMutate: (question) => { setLog((l) => [...l, { q: question }]); setQ(""); },
    onSuccess: (r) => setLog((l) =>
      l.map((t, i) => i === l.length - 1
        ? { ...t, a: r.answer, tools: r.tools_used } : t)),
    onError: (e) => setLog((l) =>
      l.map((t, i) => i === l.length - 1 ? { ...t, err: errText(e) } : t)),
  });

  const submit = () => { const v = q.trim(); if (v && !ask.isPending) ask.mutate(v); };

  if (status.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (!status.data?.available) {
    return (
      <>
        <h1>Assistant</h1>
        <div className="note">
          The assistant needs a language model backend and none is
          configured. It appears as soon as one is: the bundled model on a
          standard self-hosted install, or an endpoint you point it
          at. <Link to="/settings/categorize">Set one up in Settings ›</Link>
        </div>
      </>
    );
  }

  return (
    <>
      <h1>Assistant</h1>
      {/* the asker sticks: turns render newest-first BELOW it, and the
          input must stay in view on the one page whose entire purpose is
          asking another. */}
      <div className="card asker">
        <div style={{ display: "flex", gap: ".5rem", flexWrap: "wrap" }}>
          <input value={q} onChange={(e) => setQ(e.target.value)}
                 onKeyDown={(e) => e.key === "Enter" && submit()}
                 placeholder="e.g. how much did I spend on groceries this year?"
                 style={{ flex: 1, minWidth: "18rem" }} />
          <button className="pri" disabled={ask.isPending || !q.trim()} onClick={submit}>
            {ask.isPending ? "thinking…" : "Ask"}
          </button>
        </div>
        {/* the chips stay after the first question — exactly when
            someone has learned what this can answer and wants another */}
        <div style={{ marginTop: ".55rem", display: "flex", gap: ".4rem",
                      flexWrap: "wrap" }}>
          {EXAMPLES.map((ex) => (
            <button key={ex} onClick={() => ask.mutate(ex)}
                    disabled={ask.isPending}
                    style={{ fontSize: 12.5, borderRadius: 999,
                             padding: ".25rem .75rem" }}>{ex}</button>
          ))}
        </div>
        <div className="sub" style={{ marginTop: ".5rem", display: "flex",
              gap: ".5rem", alignItems: "center", flexWrap: "wrap" }}>
          {status.data.local ? (<>
            <span className="pill g">local</span>
            <span>answered by the model on this server — read-only tools only,
              and nothing leaves the box.</span>
          </>) : (
            <span>read-only tools only — your questions and the figures they
              return go to {status.data.host || "the configured model host"}.</span>
          )}
        </div>
      </div>

      {[...log].reverse().map((t, i) => (
        <div className="card" key={log.length - 1 - i}>
          <div style={{ fontWeight: 600 }}>{t.q}</div>
          {t.a !== undefined && (<>
            <div style={{ marginTop: ".4rem", whiteSpace: "pre-wrap" }}>{t.a}</div>
            {/* which read-only tools fed the answer — mobile showed
                this first; provenance beats a bare paragraph */}
            {(t.tools ?? []).length > 0 && (
              <div className="sub" style={{ marginTop: ".3rem", fontSize: 12 }}>
                from {t.tools!.join(" · ")}
              </div>
            )}
            <div style={{ marginTop: ".5rem", paddingTop: ".4rem",
                          borderTop: "1px dashed var(--line)", display: "flex",
                          gap: ".5rem" }}>
              <button style={{ fontSize: 12 }}
                      onClick={() => void navigator.clipboard?.writeText(t.a!)}>
                copy</button>
              <button style={{ fontSize: 12 }}
                      onClick={() => setQ(t.q)}>ask a follow-up</button>
            </div>
          </>)}
          {t.err && <div className="note bad" style={{ marginTop: ".4rem" }}>{t.err}</div>}
          {t.a === undefined && !t.err && <div className="mut" style={{ marginTop: ".4rem" }}>…</div>}
        </div>
      ))}
    </>
  );
}
