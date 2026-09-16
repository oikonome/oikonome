// in-app Help — the shipped docs/*.md as a topic catalog with
// client-side search (title + body substring; no search service), plus a
// short FAQ. ?topic= deep-links a topic. Markdown renders with the tiny
// block renderer below — headings, paragraphs, fenced code, lists,
// tables, links, bold/inline-code — no new dependency for the rest.
import { useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { useSearchParams } from "react-router";
import { api } from "../api/client";
import { useNarrow } from "../narrow";

// the sidebar groups the topic catalog — feature guides first,
// then the setup/data docs, then reference. The server stamps each topic
// with its group; anything unstamped (older payload) reads as reference.
const GROUPS: [string, string][] = [
  ["guides", "Guides"], ["setup", "Setup & data"], ["reference", "Reference"]];
const groupOf = (t: object) => (t as { group?: string }).group ?? "reference";

export default function Help() {
  const q = useQuery({ queryKey: ["help"], queryFn: api.help,
                       staleTime: 60 * 60 * 1000 });
  const [sp, setSp] = useSearchParams();
  const [filter, setFilter] = useState("");
  // On phones the topic index folds into a picker above the article, so the
  // article is on screen first rather than below a column of links.
  const narrow = useNarrow();
  const [pickerOpen, setPickerOpen] = useState(false);
  if (q.isPending) return <p className="mut" style={{ marginTop: "2rem" }}>loading…</p>;
  if (q.isError) return <div className="note bad">Couldn't load: {String(q.error)}</div>;
  const topics = q.data.topics;
  const needle = filter.trim().toLowerCase();
  const hits = needle
    ? topics.filter((t) => t.title.toLowerCase().includes(needle)
                        || t.body.toLowerCase().includes(needle))
    : topics;
  const current = topics.find((t) => t.id === (sp.get("topic") || "faq"))
    ?? topics[0];

  const pick = (id: string) => {
    setSp({ topic: id });
    setPickerOpen(false);
    if (narrow) window.scrollTo({ top: 0 });
  };
  const index = (
    <>
      <input type="search" placeholder="Search help…" value={filter}
             style={{ width: "100%", marginBottom: ".6rem" }}
             onChange={(e) => setFilter(e.target.value)} />
      {hits.length === 0 && <p className="sub">Nothing matches.</p>}
      {GROUPS.map(([g, label]) => {
        const inGroup = hits.filter((t) => groupOf(t) === g);
        if (inGroup.length === 0) return null;
        return (
          <div key={g}>
            <p className="sub" style={{ margin: ".6rem 0 .1rem",
                                        textTransform: "uppercase",
                                        letterSpacing: ".05em" }}>
              {label}</p>
            {inGroup.map((t) => (
              <a key={t.id} href={`#${t.id}`}
                 onClick={(e) => { e.preventDefault(); pick(t.id); }}
                 style={{ display: "block", padding: ".25rem 0",
                          fontWeight: t.id === current?.id ? 700 : 400 }}>
                {t.title}</a>
            ))}
          </div>
        );
      })}
      <p className="sub" style={{ marginTop: ".8rem", marginBottom: 0 }}>
        The same pages live in the repo's <code>docs/</code> folder.</p>
    </>
  );
  const article = current ? <Markdown md={current.body} onTopic={pick} />
                          : <p className="sub">No help topics found.</p>;

  if (narrow) {
    return (
      <>
        <h1>Help</h1>
        <details className="card help-picker" open={pickerOpen}
                 onToggle={(e) => setPickerOpen(e.currentTarget.open)}>
          <summary>
            <span className="sub" style={{ textTransform: "uppercase",
                                           letterSpacing: ".05em" }}>Topic</span>
            <span className="help-picker-title">{current?.title ?? "Help"}</span>
            <span className="help-picker-chev" aria-hidden="true">▾</span>
          </summary>
          <div className="help-picker-body">{index}</div>
        </details>
        <div className="card" style={{ marginTop: ".8rem", minWidth: 0 }}>
          {article}
        </div>
      </>
    );
  }

  return (
    <>
      <h1>Help</h1>
      <div style={{ display: "flex", gap: "1rem", alignItems: "flex-start" }}>
        <div className="card" style={{ flex: "0 0 15rem", marginTop: 0 }}>
          {index}
        </div>
        <div className="card" style={{ flex: "1 1 0", marginTop: 0,
                                       minWidth: 0 }}>
          {article}
        </div>
      </div>
    </>
  );
}

// ---- tiny markdown renderer (block-level, safe: text nodes only) ---------

// the docs ship with the app, but keep the renderer inert on
// principle — only http(s)/mailto, #anchors and scheme-less relative
// hrefs become links; any other scheme (javascript:, data:, …) renders
// as plain text. Protocol-relative (//evil.com — and \\, /\,
// \/, which browsers normalize to //) has no scheme prefix but still
// leaves the origin, so it doesn't count as relative either.
// Judge the href the way the BROWSER will parse it — URL parsers
// strip C0 controls and spaces, so "\x01javascript:x" reads as relative to
// the regexes but executes as a scheme. Strip them before testing.
const safeHref = (raw: string) => {
  const h = raw.replace(/[\u0000-\u0020]/g, "");
  return /^(https?:|mailto:)/i.test(h)
    || (!/^[a-z][a-z0-9+.-]*:/i.test(h) && !/^[/\\]{2}/.test(h));
};

// The docs cross-link each other by file — `guides/bills.md`,
// `../community-scripts.md`, `faq.md#section`. Served from /help those
// paths lead nowhere; the topic they mean is the file's stem, which is
// exactly how the server ids a topic. Returns that stem, or null when the
// href is anything else (external, anchor, non-doc).
const docTopic = (href: string): string | null => {
  if (/^[a-z][a-z0-9+.-]*:/i.test(href) || /^[/\\]{2}/.test(href)) return null;
  const m = /(?:^|\/)([^/#?]+)\.md(?:[#?].*)?$/i.exec(href);
  return m ? m[1] : null;
};

type TopicNav = (id: string) => void;

function inline(text: string, key = 0, onTopic?: TopicNav): ReactNode[] {
  // links → bold → inline code, left to right
  const out: ReactNode[] = [];
  let rest = text, k = key;
  const rx = /\[([^\]]+)\]\(([^)\s]+)\)|\*\*([^*]+)\*\*|`([^`]+)`/;
  while (rest) {
    const m = rx.exec(rest);
    if (!m) { out.push(rest); break; }
    if (m.index > 0) out.push(rest.slice(0, m.index));
    if (m[1] !== undefined) {
      const href = m[2];
      const topic = onTopic ? docTopic(href) : null;
      out.push(topic
        ? <a key={k++} href={`?topic=${topic}`}
             onClick={(e) => { e.preventDefault(); onTopic!(topic); }}>
            {m[1]}</a>
        : safeHref(href)
        ? <a key={k++} href={href}
             target={href.startsWith("http") ? "_blank" : undefined}
             rel="noreferrer">{m[1]}</a>
        : <span key={k++}>{m[1]}</span>);
    } else if (m[3] !== undefined) {
      out.push(<b key={k++}>{inline(m[3], k * 100, onTopic)}</b>);
    } else {
      out.push(<code key={k++}>{m[4]}</code>);
    }
    rest = rest.slice(m.index + m[0].length);
  }
  return out;
}

function Markdown({ md, onTopic }: { md: string; onTopic?: TopicNav }) {
  const lines = md.split("\n");
  const out: ReactNode[] = [];
  let i = 0, k = 0;
  const paras: string[] = [];
  const flush = () => {
    if (paras.length) {
      out.push(<p key={k++}>{inline(paras.join(" "), 0, onTopic)}</p>);
      paras.length = 0;
    }
  };
  while (i < lines.length) {
    const line = lines[i];
    if (line.startsWith("```")) {
      flush();
      const code: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) code.push(lines[i++]);
      i++;
      out.push(<pre key={k++} style={{ overflowX: "auto" }}>
        <code>{code.join("\n")}</code></pre>);
      continue;
    }
    if (/^#{1,4} /.test(line)) {
      flush();
      const level = line.match(/^#+/)![0].length;
      const text = line.replace(/^#+\s*/, "");
      out.push(level <= 1 ? <h2 key={k++}>{inline(text, 0, onTopic)}</h2>
        : level === 2 ? <h3 key={k++}>{inline(text, 0, onTopic)}</h3>
        : <h4 key={k++}>{inline(text, 0, onTopic)}</h4>);
      i++;
      continue;
    }
    if (/^\s*([-*]|\d+\.) /.test(line)) {
      flush();
      const items: string[] = [];
      const ordered = /^\s*\d+\. /.test(line);
      while (i < lines.length && /^\s*([-*]|\d+\.) /.test(lines[i])) {
        let item = lines[i].replace(/^\s*([-*]|\d+\.)\s*/, "");
        i++;
        // continuation lines (indented) belong to the same item
        while (i < lines.length && /^\s{2,}\S/.test(lines[i])
               && !/^\s*([-*]|\d+\.) /.test(lines[i])) {
          item += " " + lines[i].trim();
          i++;
        }
        items.push(item);
      }
      const els = items.map((it, j) => <li key={j}>{inline(it, 0, onTopic)}</li>);
      out.push(ordered ? <ol key={k++}>{els}</ol> : <ul key={k++}>{els}</ul>);
      continue;
    }
    if (line.startsWith("|")) {
      flush();
      const rows: string[][] = [];
      while (i < lines.length && lines[i].startsWith("|")) {
        const cells = lines[i].split("|").slice(1, -1).map((c) => c.trim());
        if (!cells.every((c) => /^:?-+:?$/.test(c))) rows.push(cells);
        i++;
      }
      out.push(
        <div key={k++} style={{ overflowX: "auto" }}><table>
          {rows.length > 0 && (
            <thead><tr>{rows[0].map((c, j) =>
              <th key={j}>{inline(c, 0, onTopic)}</th>)}</tr></thead>
          )}
          <tbody>{rows.slice(1).map((r, ri) => (
            <tr key={ri}>{r.map((c, j) => <td key={j}>{inline(c, 0, onTopic)}</td>)}</tr>
          ))}</tbody>
        </table></div>);
      continue;
    }
    if (line.startsWith(">")) {           // blockquote → muted paragraph
      flush();
      const quote: string[] = [];
      while (i < lines.length && lines[i].startsWith(">"))
        quote.push(lines[i++].replace(/^>\s?/, ""));
      out.push(<p key={k++} className="mut">{inline(quote.join(" "), 0, onTopic)}</p>);
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    paras.push(line.trim());
    i++;
  }
  flush();
  return <>{out}</>;
}
