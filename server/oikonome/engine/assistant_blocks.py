"""The assistant's charts and tables.

The model may put a picture in an answer — a trend by month, a split by
category, a small table — but it never gets to write markup. It emits a
fenced ```chart block holding a small JSON spec; this module pulls those
out of the reply text, validates every field against a strict shape (a
handful of chart types, bounded series/label/row counts, numbers only in
the number slots, short strings) and hands the clients a list of typed
blocks. An invalid block is never half-rendered: the reader gets a short
notice in its place, and the prose around it is kept. The same validated
blocks render into a self-contained HTML report (inline SVG, inline CSS,
no script) the user can save or share.
"""

from __future__ import annotations

import html
import json
import math
import re

from ..web import charts

CHART_TYPES = ("bar", "line", "pie", "table")
MAX_BLOCKS = 4
MAX_SERIES = 4
MAX_LABELS = 60
MAX_TABLE_COLUMNS = 8
MAX_TABLE_ROWS = 60
MAX_TITLE = 120
MAX_CELL = 80

# ```chart … ``` or ```json … ```: small local models mislabel the fence,
# and a json block whose object carries a chart "type" is still a chart
_FENCE_RE = re.compile(r"```(?:chart|json)?[ \t]*\n(.*?)```", re.S)

_SPEC_GUIDE = (
    "When a picture or a table makes the answer clearer — a trend over "
    "months or years, a comparison, a breakdown — add one as a fenced "
    "block, prose outside it:\n"
    "```chart\n"
    '{"type": "bar", "title": "Groceries by month, 2025", '
    '"labels": ["Jan", "Feb"], '
    '"series": [{"name": "Groceries", "values": [412.5, 388.0]}]}\n'
    "```\n"
    '"type" is "bar", "line" or "pie" (pie: one series, parts of a whole), '
    'or "table" with "columns" and "rows":\n'
    "```chart\n"
    '{"type": "table", "title": "Top merchants", '
    '"columns": ["Merchant", "Spent"], "rows": [["Example Mart", 1204.1]]}\n'
    "```\n"
    "Chart values are US dollars as plain numbers copied from a tool "
    "result. In a table write numbers as numbers and put the unit in the "
    'column name ("Spent, $", "Visits"). At most a few blocks per answer, '
    "each with a short title."
)


def guide() -> str:
    """The system-prompt paragraph that teaches the model the block shape."""
    return _SPEC_GUIDE


# ---- validation -----------------------------------------------------------

def _num(v):
    """A finite number, or None. Booleans are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:
        # a JSON integer too long for a float (hundreds of digits) is no
        # figure anyone meant; refusing it drops this one value, where
        # letting float() raise would fail the whole answer
        return None
    if not math.isfinite(f):
        return None
    return round(f, 2)


def _text(v, cap: int) -> str | None:
    if v is None:
        return ""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        n = _num(v)
        return None if n is None else (str(int(n)) if n == int(n)
                                       else f"{n:.2f}")
    if not isinstance(v, str):
        return None
    s = " ".join(v.split())
    return s[:cap]


def validate(obj) -> dict | None:
    """A chart/table block in its canonical shape, or None when `obj` is
    not one. Strict on purpose: the block came from a model and goes to
    three renderers."""
    if not isinstance(obj, dict):
        return None
    kind = obj.get("type")
    if kind not in CHART_TYPES:
        return None
    title = _text(obj.get("title"), MAX_TITLE)
    if title is None:
        return None
    if kind == "table":
        cols = obj.get("columns")
        rows = obj.get("rows")
        if not isinstance(cols, list) or not cols \
                or len(cols) > MAX_TABLE_COLUMNS or not isinstance(rows, list):
            return None
        columns = [_text(c, MAX_CELL) for c in cols]
        if any(c is None for c in columns):
            return None
        out_rows = []
        for r in rows[:MAX_TABLE_ROWS]:
            if not isinstance(r, list) or len(r) != len(columns):
                return None
            cells = []
            for c in r:
                n = _num(c)
                if n is not None:
                    cells.append(n)
                    continue
                t = _text(c, MAX_CELL)
                if t is None:
                    return None
                cells.append(t)
            out_rows.append(cells)
        if not out_rows:
            return None
        return {"kind": "table", "title": title, "columns": columns,
                "rows": out_rows}
    labels = obj.get("labels")
    series = obj.get("series")
    if not isinstance(labels, list) or not labels or len(labels) > MAX_LABELS:
        return None
    labs = [_text(l, MAX_CELL) for l in labels]
    if any(l is None for l in labs):
        return None
    if not isinstance(series, list) or not series or len(series) > MAX_SERIES:
        return None
    if kind == "pie" and len(series) != 1:
        return None
    out_series = []
    for s in series:
        if not isinstance(s, dict):
            return None
        name = _text(s.get("name"), MAX_CELL)
        vals = s.get("values")
        if name is None or not isinstance(vals, list) \
                or len(vals) != len(labs):
            return None
        nums = [_num(v) for v in vals]
        if any(n is None for n in nums):
            return None
        out_series.append({"name": name, "values": nums})
    return {"kind": kind, "title": title, "labels": labs,
            "series": out_series}


# what a reader sees where a chart the model wrote cannot be drawn: an
# answer must never quietly lose part of itself, nor show raw JSON
DROPPED_NOTICE = "(A chart for this answer could not be drawn.)"


def parse(answer: str) -> tuple[str, list[dict]]:
    """Split a reply into its prose and its validated blocks.

    Returns (prose, blocks): `prose` is the answer with every fenced block
    removed (so an older client that only reads `answer` still gets the
    sentences), `blocks` is the ordered mix of {"kind": "text"} and
    chart/table blocks the clients render in place.

    A fence that is not JSON at all is something the model quoted — a
    table typed as text, a list — and stays as plain text without its
    fence. A JSON block that is not a drawable spec, or a chart past
    MAX_BLOCKS, becomes DROPPED_NOTICE in the text, so an answer made of
    nothing else is never empty and never raw JSON."""
    blocks: list[dict] = []
    prose_parts: list[str] = []
    pending = ""
    pos = 0
    charts_kept = 0

    def _push_text(t: str):
        t = t.strip()
        if t:
            prose_parts.append(t)
            blocks.append({"kind": "text", "text": t})

    answer = answer or ""
    for m in _FENCE_RE.finditer(answer):
        pending += answer[pos:m.start()]
        pos = m.end()
        body = m.group(1)
        try:
            obj = json.loads(body)
        except (ValueError, TypeError):
            pending += "\n" + body.strip() + "\n"
            continue
        spec = validate(obj)
        if spec is None or charts_kept >= MAX_BLOCKS:
            pending += "\n" + DROPPED_NOTICE + "\n"
            continue
        _push_text(pending)
        pending = ""
        blocks.append(spec)
        charts_kept += 1
    _push_text(pending + answer[pos:])
    return "\n\n".join(prose_parts), blocks


def clean(blocks) -> list[dict]:
    """Re-validate blocks a client sends back (for the report): a text
    block keeps its text, trimmed; anything else must pass `validate`."""
    out: list[dict] = []
    if not isinstance(blocks, list):
        return out
    kept = 0
    for b in blocks[:MAX_BLOCKS * 4]:
        if not isinstance(b, dict):
            continue
        if b.get("kind") == "text":
            t = b.get("text")
            if isinstance(t, str) and t.strip():
                out.append({"kind": "text", "text": t.strip()[:6000]})
            continue
        if kept >= MAX_BLOCKS:
            continue
        spec = validate({**b, "type": b.get("kind")})
        if spec is not None:
            out.append(spec)
            kept += 1
    return out


# ---- the HTML report ---------------------------------------------------------

_CSS = """
body{margin:0;background:#f2f5f7;color:#1f2933;font:15px/1.5 -apple-system,
"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
main{max-width:760px;margin:0 auto;padding:28px 16px 48px}
h1{font-size:20px;margin:0 0 4px}
.sub{color:#5b6875;font-size:13px;margin-bottom:20px}
.card{background:#fff;border:1px solid #d8dfe6;border-radius:12px;
padding:16px 18px;margin:0 0 14px}
.card h2{font-size:15px;margin:0 0 10px;font-weight:600}
p{margin:0 0 10px;white-space:pre-wrap}
svg{display:block;max-width:100%;height:auto}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid #d8dfe6}
th{color:#5b6875;font-weight:600;font-size:12.5px}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
.lg{display:flex;align-items:center;gap:8px;font-size:13px;margin:4px 0}
.sw{width:10px;height:10px;border-radius:3px;display:inline-block}
.legend{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:8px}
.mut{color:#5b6875}
.foot{color:#5b6875;font-size:12px;margin-top:20px}
.donut{display:flex;gap:20px;align-items:center;flex-wrap:wrap}
.donut .legend{display:block}
.dc-t{font-size:17px;font-weight:600;fill:#1f2933}
.dc-s{font-size:11px;fill:#5b6875}
"""

INK = charts.INK
MUTED = charts.MUTED
PALETTE = charts.PALETTE


def _money(v: float) -> str:
    return charts._money(v)


def _short(v: float) -> str:
    return charts._short_money(v)


def _svg_bar(spec: dict, w: int = 720, h: int = 260) -> str:
    labels, series = spec["labels"], spec["series"]
    n, k = len(labels), len(series)
    allv = [v for s in series for v in s["values"]] + [0.0]
    ymin, ymax = min(allv), max(allv)
    span = (ymax - ymin) or 1.0
    padL, padR, padT, padB = 60, 12, 14, 40
    iw, ih = w - padL - padR, h - padT - padB
    group = iw / n
    gap = min(8.0, group * 0.15)
    bw = max(2.0, (group - gap) / k - (2 if k > 1 else 0))

    def Y(v: float) -> float:
        return padT + ih - ih * (v - ymin) / span
    parts = []
    for t in (ymax, (ymax + ymin) / 2, ymin):
        y = Y(t)
        parts.append(f'<line x1="{padL}" x2="{w - padR}" y1="{y:.1f}" '
                     f'y2="{y:.1f}" stroke="#d8dfe6"/>'
                     f'<text x="{padL - 6}" y="{y + 4:.1f}" text-anchor="end" '
                     f'font-size="11" fill="{MUTED}">{_short(t)}</text>')
    y0 = Y(0)
    parts.append(f'<line x1="{padL}" x2="{w - padR}" y1="{y0:.1f}" '
                 f'y2="{y0:.1f}" stroke="{MUTED}" stroke-opacity=".7"/>')
    for i, lab in enumerate(labels):
        gx = padL + group * i + gap / 2
        for j, s in enumerate(series):
            v = s["values"][i]
            x = gx + j * (bw + (2 if k > 1 else 0))
            top, bot = (Y(v), y0) if v >= 0 else (y0, Y(v))
            parts.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{bw:.1f}" '
                f'height="{max(0.0, bot - top):.1f}" rx="2" '
                f'fill="{PALETTE[j % len(PALETTE)]}"/>')
            if k == 1 and n <= 16:
                parts.append(
                    f'<text x="{x + bw / 2:.1f}" y="{top - 4:.1f}" '
                    f'text-anchor="middle" font-size="10.5" fill="{INK}">'
                    f'{_short(v)}</text>')
        if n <= 24 or i % max(1, n // 12) == 0:
            parts.append(
                f'<text x="{padL + group * i + group / 2:.1f}" '
                f'y="{h - padB + 16}" text-anchor="middle" font-size="11" '
                f'fill="{MUTED}">{html.escape(_shorten(lab, 12))}</text>')
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img" aria-label="{html.escape(spec["title"])}">'
            + "".join(parts) + "</svg>")


def _shorten(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _svg_line(spec: dict) -> str:
    series = spec["series"]
    labels = spec["labels"]
    pts = [(lab, s_v) for lab, s_v in zip(labels, series[0]["values"])]
    second = [(lab, v) for lab, v in zip(labels, series[1]["values"])] \
        if len(series) > 1 else None
    third = [(lab, v) for lab, v in zip(labels, series[2]["values"])] \
        if len(series) > 2 else None
    # a fourth series has no slot in the shared line renderer; the legend
    # and the table view under the chart still carry it
    return charts.line(pts, h=240, interactive=False, fill=len(series) == 1,
                       second=second,
                       second_color=PALETTE[1], third=third,
                       third_color=PALETTE[2])


def _legend(series: list[dict]) -> str:
    if len(series) < 2:
        return ""
    return ('<div class="legend">' + "".join(
        f'<div class="lg"><span class="sw" style="background:'
        f'{PALETTE[i % len(PALETTE)]}"></span>{html.escape(s["name"])}</div>'
        for i, s in enumerate(series)) + "</div>")


def _plain(v: float) -> str:
    """A table number: a count, a percentage, a dollar figure — the column
    name carries the unit, so 52 visits are not written as $52."""
    return f"{v:,.0f}" if v == int(v) else f"{v:,.2f}"


def _table_html(columns: list[str], rows: list[list], fmt=_plain) -> str:
    head = "".join(
        f'<th class="{"n" if _numeric_col(rows, i) else ""}">'
        f'{html.escape(c)}</th>' for i, c in enumerate(columns))
    body = []
    for r in rows:
        cells = []
        for i, c in enumerate(r):
            if isinstance(c, float):
                cells.append(f'<td class="n">{fmt(c)}</td>')
            else:
                cells.append(f'<td>{html.escape(str(c))}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (f"<table><thead><tr>{head}</tr></thead><tbody>"
            + "".join(body) + "</tbody></table>")


def _numeric_col(rows: list[list], i: int) -> bool:
    return bool(rows) and all(isinstance(r[i], float) for r in rows)


def _data_table(spec: dict) -> str:
    """Every chart ships its numbers as a table too — the picture is never
    the only way to read a figure."""
    cols = [""] + [s["name"] for s in spec["series"]]
    rows = [[lab] + [s["values"][i] for s in spec["series"]]
            for i, lab in enumerate(spec["labels"])]
    return _table_html(cols, rows, fmt=_money)


def render_block(b: dict) -> str:
    kind = b.get("kind")
    if kind == "text":
        return f'<div class="card"><p>{html.escape(b["text"])}</p></div>'
    title = html.escape(b.get("title") or "")
    head = f"<h2>{title}</h2>" if title else ""
    if kind == "table":
        return (f'<div class="card">{head}'
                f'{_table_html(b["columns"], b["rows"])}</div>')
    if kind == "pie":
        segs = list(zip(b["labels"], b["series"][0]["values"]))
        return (f'<div class="card">{head}{charts.donut(segs)}</div>')
    if kind == "line":
        return (f'<div class="card">{head}{_svg_line(b)}'
                f'{_legend(b["series"])}{_data_table(b)}</div>')
    if kind == "bar":
        return (f'<div class="card">{head}{_svg_bar(b)}'
                f'{_legend(b["series"])}{_data_table(b)}</div>')
    return ""


def report_html(question: str, blocks: list[dict], today: str) -> str:
    """A self-contained page for one answer: the question, the prose and
    the charts, every number also in a table. Inline CSS + SVG only —
    it opens the same from a Downloads folder, a mail attachment or a
    phone's share sheet."""
    q = html.escape((question or "").strip()[:1000]) or "Assistant"
    body = "".join(render_block(b) for b in blocks)
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        f"<title>{q}</title><style>{_CSS}</style></head>"
        f"<body><main><h1>{q}</h1>"
        f"<div class=\"sub\">Oikonome assistant · {html.escape(today)}</div>"
        f"{body}"
        "<div class=\"foot\">Figures come from the ledger as it stood when "
        "the question was asked; the model phrased them and drew the "
        "charts from them.</div></main></body></html>")
