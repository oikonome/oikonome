// The little markdown the assistant writes, parsed for drawing. The
// model answers in prose with **bold** figures and "- " bullet lists;
// shown raw, the asterisks read as noise. This is the whole subset —
// bold, bullet and numbered lists, a "# heading" line read as a bold
// paragraph — and nothing else is markup: a stray asterisk, an
// underscore in a name, a "<b>" the model typed all stay text. A
// numbered item keeps ITS OWN number (up to nine digits; a ranking split around a chart,
// or spaced by blank lines, still reads 1, 2, 3), and a line is a
// numbered item only when it is numbered itself. The web twin is
// webapp/src/richtext.ts (same code) and the server parses the same
// subset the same way for the HTML report (assistant_blocks.parse_rich;
// the suite compares the two on shared fixtures), so an answer reads the
// same on every surface. Pure: no React, no RN.

export type Run = { bold: boolean; text: string };
export type Item = { runs: Run[]; n: number | null };
export type RichBlock =
  | { kind: "p"; runs: Run[] }
  | { kind: "list"; ordered: boolean; items: Item[] };

const BULLET_RE = /^\s*(?:([-*•])|(\d{1,9})[.)])\s+(.*)$/;
const HEADING_RE = /^\s*#{1,6}\s+(.*)$/;
const BOLD_RE = /\*\*(.+?)\*\*/g;

/** one line's runs: the text between the bold spans, and the bold spans */
export function inlineRuns(line: string, bold = false): Run[] {
  const out: Run[] = [];
  let pos = 0;
  for (const m of line.matchAll(BOLD_RE)) {
    const at = m.index ?? 0;
    if (at > pos) out.push({ bold, text: line.slice(pos, at) });
    out.push({ bold: true, text: m[1] });
    pos = at + m[0].length;
  }
  if (pos < line.length || out.length === 0) out.push({ bold, text: line.slice(pos) });
  return out;
}

/** a paragraph's lines joined with the line breaks they came with: the
 *  model's own breaks inside a paragraph are kept, as the raw text was */
function joinLines(lines: Run[][]): Run[] {
  const out: Run[] = [];
  lines.forEach((runs, i) => {
    if (i) out.push({ bold: false, text: "\n" });
    out.push(...runs);
  });
  return out;
}

export function parseRich(text: string): RichBlock[] {
  const out: RichBlock[] = [];
  let para: Run[][] = [];
  let items: Item[] = [];
  let ordered = false;
  const flushPara = () => { if (para.length) { out.push({ kind: "p", runs: joinLines(para) }); para = []; } };
  const flushList = () => { if (items.length) { out.push({ kind: "list", ordered, items }); items = []; } ordered = false; };
  for (const raw of (text ?? "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) { flushPara(); flushList(); continue; }
    const b = BULLET_RE.exec(line);
    if (b) {
      flushPara();
      const numbered = b[2] !== undefined;
      // a bullet after numbered items (or the reverse) is a new list
      if (items.length && numbered !== ordered) flushList();
      if (!items.length) ordered = numbered;
      items.push({ runs: inlineRuns(b[3].trim()), n: numbered ? Number(b[2]) : null });
      continue;
    }
    flushList();
    const h = HEADING_RE.exec(line);
    if (h) { flushPara(); out.push({ kind: "p", runs: inlineRuns(h[1].trim(), true) }); continue; }
    para.push(inlineRuns(line));
  }
  flushPara(); flushList();
  return out;
}

/** the number an ordered item shows: its own */
export const itemLabel = (it: Item, i: number) => it.n === null ? `${i + 1}.` : `${it.n}.`;

/** the text with its markup gone — what "copy" puts on the clipboard,
 *  so a pasted answer carries no asterisks either */
export function plainText(text: string): string {
  return parseRich(text).map((b) => b.kind === "p"
    ? b.runs.map((r) => r.text).join("")
    : b.items.map((it, i) => `${b.ordered ? itemLabel(it, i) : "-"} ${it.runs.map((r) => r.text).join("")}`).join("\n"))
    .join("\n\n");
}
