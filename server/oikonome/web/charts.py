"""Self-contained server-rendered SVG charts (no external JS/CDN). Palette
matches the daily email's design language; categorical colors are CVD-safe."""
from __future__ import annotations

import html
import json

# CVD-safe categorical palette (blue/orange/teal/purple/green/amber/red/muted)
PALETTE = ["#4f9bd6", "#e8833a", "#3a9188", "#8a5fb0", "#4f9d5b",
           "#d9a441", "#c65c4e", "#7a8a99", "#6ab0d6", "#b0894a"]
INK = "#37424a"
MUTED = "#8a97a1"
GREEN = "#4f9d5b"
RED = "#c65c4e"

_MON = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


PRIMARY = "#4f9bd6"        # one accent for every chart (matches --blue)

_UID = [0]                 # unique gradient ids across many charts on one page
def _uid() -> int:
    _UID[0] += 1
    return _UID[0]


def _short_money(v: float) -> str:
    """Axis labels: $1.2M / $340k — matches the interactive chart's y-axis."""
    a = abs(v)
    if a >= 1e6:
        s = f"${v / 1e6:.2f}".rstrip("0").rstrip(".") + "M"
        return s
    if a >= 1e3:
        return f"${v / 1e3:,.0f}k"
    return f"${v:,.0f}"


def _pretty_label(lab) -> str:
    """'2022-03' -> 'Mar 2022'; anything else passes through."""
    s = str(lab)
    if len(s) == 7 and s[4] == "-":
        try:
            return f"{_MON[int(s[5:7])]} {s[:4]}"
        except (ValueError, IndexError):
            pass
    return s


def _money(v: float) -> str:
    n = round(v)
    return ("-$" if n < 0 else "$") + f"{abs(n):,}"


def donut(segments: list[tuple[str, float]], size: int = 180) -> str:
    """segments: [(label, value)] — negatives dropped. Returns SVG + legend HTML."""
    segs = [(l, v) for l, v in segments if v and v > 0]
    total = sum(v for _, v in segs) or 1
    r, cx, cy, sw = size / 2 - 14, size / 2, size / 2, 26
    import math
    circ = 2 * math.pi * r
    off = 0.0
    parts = []
    legend = []
    for i, (label, val) in enumerate(segs):
        frac = val / total
        color = PALETTE[i % len(PALETTE)]
        dash = frac * circ
        parts.append(
            f'<circle cx="{cx}" cy="{cy}" r="{r:.1f}" fill="none" stroke="{color}" '
            f'stroke-width="{sw}" stroke-dasharray="{dash:.2f} {circ - dash:.2f}" '
            f'stroke-dashoffset="{-off:.2f}" transform="rotate(-90 {cx} {cy})"/>')
        off += dash
        legend.append(
            f'<div class="lg"><span class="sw" style="background:{color}"></span>'
            f'{html.escape(label)} <b>{_money(val)}</b> '
            f'<span class="mut">{frac*100:.0f}%</span></div>')
    svg = (f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}">'
           + "".join(parts) +
           f'<text x="{cx}" y="{cy-2}" text-anchor="middle" class="dc-t">{_money(total)}</text>'
           f'<text x="{cx}" y="{cy+16}" text-anchor="middle" class="dc-s">total</text></svg>')
    return f'<div class="donut">{svg}<div class="legend">{"".join(legend)}</div></div>'


def line(points: list[tuple[str, float]], w: int = 720, h: int = 240,
         fmt=_short_money, color: str = PRIMARY, fill: bool = True,
         estimated_until: int | None = None, xticks: str | None = None,
         hover: bool = False, interactive: bool = True,
         zero_line: bool = False, mark_x: int | None = None,
         mark_label: str = "", neg_fill: bool = False,
         second: list[tuple[str, float]] | None = None,
         second_color: str = "#e8833a",
         third: list[tuple[str, float]] | None = None,
         third_color: str = "#5cb56b",
         events: list[tuple[int, str]] | None = None,
         labels: tuple | None = None) -> str:
    """points: [(x_label, y)]. Renders an area/line chart with a few axis labels.

    estimated_until: if set, points[0..estimated_until] are drawn as a hatched,
    dashed "reconstructed estimate" segment and points[estimated_until..] solid
    (recorded). The two share the boundary point so the line stays continuous.
    xticks='years': one x label per calendar year (labels 'YYYY-MM' points).
    hover=True: pure-CSS tooltip (used only in the static fallback).
    interactive=True (default): wrap in a `.linechart` div carrying the data so
    the base template's JS upgrades it to a drag-to-zoom / crosshair chart. The
    server SVG stays inside as a no-JS fallback (rendered without the hover layer,
    since the JS provides hover)."""
    if interactive:
        hover = False
    if not points:
        return '<div class="mut" style="padding:2rem">No data yet.</div>'
    ys = ([p[1] for p in points] + ([p[1] for p in second] if second else [])
          + ([p[1] for p in third] if third else []))
    ymin, ymax = min(ys + [0]), max(ys + [0])
    span = (ymax - ymin) or 1
    padL, padR, padT, padB = 64, 12, 12, 26
    iw, ih = w - padL - padR, h - padT - padB
    n = len(points)
    def X(i): return padL + (iw * i / max(1, n - 1))
    def Y(v): return padT + ih - (ih * (v - ymin) / span)
    pts = [(X(i), Y(p[1])) for i, p in enumerate(points)]
    poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    # neg_fill: split the fill AND the line at the zero line — blue above, red
    # below — so negative months read unmistakably as "in the negative".
    grad_defs = ""
    line_stroke = color
    area_fill, area_op = color, "0.12"
    neg_zero = neg_fill and ymin < 0 < ymax
    if neg_zero:
        frac = max(0.0, min(1.0, (Y(0) - padT) / ih))
        fg, sg = f"cffg{_uid()}", f"cfsg{_uid()}"
        def _stops(op):
            return (f'<stop offset="0" stop-color="{color}" stop-opacity="{op}"/>'
                    f'<stop offset="{frac:.4f}" stop-color="{color}" stop-opacity="{op}"/>'
                    f'<stop offset="{frac:.4f}" stop-color="{RED}" stop-opacity="{op}"/>'
                    f'<stop offset="1" stop-color="{RED}" stop-opacity="{op}"/>')
        grad_defs = (
            f'<linearGradient id="{fg}" gradientUnits="userSpaceOnUse" '
            f'x1="0" y1="{padT}" x2="0" y2="{padT+ih:.1f}">{_stops("0.16")}</linearGradient>'
            f'<linearGradient id="{sg}" gradientUnits="userSpaceOnUse" '
            f'x1="0" y1="{padT}" x2="0" y2="{padT+ih:.1f}">{_stops("1")}</linearGradient>')
        area_fill, area_op, line_stroke = f"url(#{fg})", "1", f"url(#{sg})"
    area = (f'<polygon points="{X(0):.1f},{Y(0):.1f} {poly} {X(n-1):.1f},{Y(0):.1f}" '
            f'fill="{area_fill}" fill-opacity="{area_op}"/>') if fill else ""
    # gridlines / y labels (0, mid, max)
    grid = []
    for gv in sorted({ymin, (ymin+ymax)/2, ymax, 0}):
        gy = Y(gv)
        grid.append(f'<line x1="{padL}" y1="{gy:.1f}" x2="{w-padR}" y2="{gy:.1f}" '
                    f'class="cgrid"/><text x="{padL-6}" y="{gy+3:.1f}" '
                    f'text-anchor="end" class="ax">{fmt(gv)}</text>')
    # x labels — one per year, or a few evenly spaced
    xlab = []
    if xticks == "years":
        seen, year_list = set(), []
        for i, p in enumerate(points):
            yr = str(p[0])[:4]
            if yr not in seen:
                seen.add(yr); year_list.append((i, yr))
        thin = len(year_list) > 16          # too many years → label only multiples of 5
        for i, yr in year_list:
            if thin and int(yr) % 5 != 0:
                continue
            xx = X(i)
            xlab.append(f'<line x1="{xx:.1f}" y1="{padT+ih:.1f}" x2="{xx:.1f}" '
                        f'y2="{padT+ih+4:.1f}" class="cgrid"/>'
                        f'<text x="{xx:.1f}" y="{h-8}" text-anchor="middle" class="ax">{yr}</text>')
    else:
        step = max(1, n // 6)
        for i in range(0, n, step):
            xlab.append(f'<text x="{X(i):.1f}" y="{h-8}" text-anchor="middle" class="ax">'
                        f'{html.escape(str(points[i][0]))}</text>')
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.5" '
        f'fill="{RED if (neg_zero and points[i][1] < 0) else color}"/>'
        for i, (x, y) in enumerate(pts)) if n <= 40 else ""

    # split into estimated (hatched + dashed) and recorded (solid) segments
    eu = None if estimated_until is None else max(0, min(estimated_until, n - 1))
    defs = band = ""
    if eu is not None and eu > 0:
        defs = ('<defs><pattern id="estHatch" width="6" height="6" '
                'patternTransform="rotate(45)" patternUnits="userSpaceOnUse">'
                f'<rect width="6" height="6" fill="{color}" fill-opacity="0.04"/>'
                f'<line x1="0" y1="0" x2="0" y2="6" stroke="{color}" '
                'stroke-width="1" stroke-opacity="0.16"/></pattern></defs>')
        band = (f'<rect x="{X(0):.1f}" y="{padT}" width="{X(eu)-X(0):.1f}" '
                f'height="{ih:.1f}" fill="url(#estHatch)"/>'
                f'<text x="{(X(0)+X(eu))/2:.1f}" y="{padT+13}" text-anchor="middle" '
                f'class="ax" fill="{MUTED}">reconstructed estimate</text>')
        est = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts[:eu + 1])
        act = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts[eu:])
        lines = (f'<polyline points="{est}" fill="none" stroke="{color}" '
                 f'stroke-width="2.5" stroke-opacity="0.65" stroke-dasharray="5 4"/>'
                 f'<polyline points="{act}" fill="none" stroke="{color}" stroke-width="2.5"/>')
        # emphasize the exact "today" anchor
        ax_, ay_ = pts[-1]
        dots += (f'<circle cx="{ax_:.1f}" cy="{ay_:.1f}" r="3.5" fill="{color}"/>')
    else:
        lines = f'<polyline points="{poly}" fill="none" stroke="{line_stroke}" stroke-width="2.5"/>'

    # optional second/third series (same x positions/scale), dashed, with a
    # legend — dash patterns differ so identity never rides on color alone
    if second:
        m2 = min(len(second), n)
        poly2 = " ".join(f"{X(i):.1f},{Y(second[i][1]):.1f}" for i in range(m2))
        lines += (f'<polyline points="{poly2}" fill="none" stroke="{second_color}" '
                  f'stroke-width="2" stroke-dasharray="6 4"/>')
    if third:
        m3 = min(len(third), n)
        poly3 = " ".join(f"{X(i):.1f},{Y(third[i][1]):.1f}" for i in range(m3))
        lines += (f'<polyline points="{poly3}" fill="none" stroke="{third_color}" '
                  f'stroke-width="2" stroke-dasharray="2 3"/>')
    if (second or third) and labels:
        lx0 = w - padR - 205    # top-right; mark_label owns the top-left
        entries = [(color, "2.5", None, labels[0])]
        if second and len(labels) > 1:
            entries.append((second_color, "2", "6 4", labels[1]))
        if third and len(labels) > 2:
            entries.append((third_color, "2", "2 3", labels[2]))
        for i, (col, sw2, dash, lab) in enumerate(entries):
            ly = padT + 8 + i * 14
            dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
            lines += (
                f'<line x1="{lx0}" y1="{ly}" x2="{lx0+18}" y2="{ly}" '
                f'stroke="{col}" stroke-width="{sw2}"{dash_attr}/>'
                f'<text x="{lx0+23}" y="{ly+3}" font-size="10" class="ax">'
                f'{html.escape(lab)}</text>')

    # pure-CSS hover: one transparent full-height column per point reveals a
    # guide line, a dot on the data point, and a value box. No JS.
    hov = ""
    if hover and n > 1:
        colw = iw / (n - 1)
        parts = ['<style>.pt .tip{opacity:0;transition:opacity .07s}'
                 '.pt:hover .tip{opacity:1}.pt .ht{fill:#000;fill-opacity:0}</style>']
        for i, (lab, v) in enumerate(points):
            xx, yy = pts[i]
            txt = f"{_pretty_label(lab)}   {fmt(v)}"
            bw = max(52.0, len(txt) * 6.1 + 12)
            bx = min(max(xx - bw / 2, padL), w - padR - bw)
            parts.append(
                f'<g class="pt"><rect class="ht" x="{xx-colw/2:.1f}" y="{padT}" '
                f'width="{colw:.2f}" height="{ih:.1f}"/>'
                f'<g class="tip" pointer-events="none">'
                f'<line x1="{xx:.1f}" y1="{padT}" x2="{xx:.1f}" y2="{padT+ih:.1f}" '
                f'stroke="{MUTED}" stroke-opacity="0.45" stroke-dasharray="3 3"/>'
                f'<circle cx="{xx:.1f}" cy="{yy:.1f}" r="3.5" fill="{color}"/>'
                f'<rect x="{bx:.1f}" y="{padT}" width="{bw:.1f}" height="16" rx="3" fill="{INK}"/>'
                f'<text x="{bx+bw/2:.1f}" y="{padT+11.5:.1f}" text-anchor="middle" '
                f'fill="#fff" font-size="11">{html.escape(txt)}</text></g></g>')
        hov = "".join(parts)
    # emphasized "run out of money" zero threshold + critical-date marker
    crit = ""
    if neg_zero:   # subtle zero baseline so the blue→red crossover reads clearly
        zy = Y(0)
        crit += (f'<line x1="{padL}" y1="{zy:.1f}" x2="{w-padR}" y2="{zy:.1f}" '
                 f'stroke="{MUTED}" stroke-width="1" stroke-dasharray="4 3" stroke-opacity="0.55"/>')
    if zero_line:
        zy = Y(0)
        crit += (f'<line x1="{padL}" y1="{zy:.1f}" x2="{w-padR}" y2="{zy:.1f}" '
                 f'stroke="{RED}" stroke-width="1.5" stroke-dasharray="5 3" stroke-opacity="0.85"/>'
                 f'<text x="{w-padR:.1f}" y="{zy-4:.1f}" text-anchor="end" '
                 f'fill="{RED}" font-size="10">$0 · out of money</text>')
    # dated events (card autopays) sit behind the critical marker: an
    # autopay is scheduled, running out of money is not. Mirrors the web
    # AreaChart's `events` and the app sparkline's, so the mail shows the
    # same lines the Today page does.
    ev_layer = ""
    for ei, elabel in (events or []):
        if not 0 <= ei < n:
            continue
        ex = pts[ei][0]
        ev_layer += (f'<line x1="{ex:.1f}" y1="{padT:.1f}" x2="{ex:.1f}" '
                     f'y2="{padT+ih:.1f}" stroke="{MUTED}" stroke-width="1" '
                     f'stroke-dasharray="3 3" stroke-opacity="0.75"/>'
                     f'<text x="{ex-3:.1f}" y="{padT+3:.1f}" fill="{MUTED}" '
                     f'font-size="9" text-anchor="start" '
                     f'transform="rotate(90 {ex-3:.1f} {padT+3:.1f})">'
                     f'{html.escape(elabel)}</text>')
    if mark_x is not None and 0 <= mark_x < n:
        mx, my = pts[mark_x]
        anchor = "start" if mark_x < n / 2 else "end"
        lx = mx + (6 if anchor == "start" else -6)
        crit += (f'<line x1="{mx:.1f}" y1="{padT:.1f}" x2="{mx:.1f}" y2="{padT+ih:.1f}" '
                 f'stroke="{RED}" stroke-width="1.5" stroke-dasharray="5 3"/>'
                 f'<circle cx="{mx:.1f}" cy="{my:.1f}" r="4.5" fill="{RED}"/>')
        if mark_label:
            crit += (f'<text x="{lx:.1f}" y="{padT+11:.1f}" text-anchor="{anchor}" '
                     f'fill="{RED}" font-size="11" font-weight="600">{html.escape(mark_label)}</text>')
    svg_out = (f'<svg viewBox="0 0 {w} {h}" width="100%" preserveAspectRatio="xMidYMid meet">'
               + defs + grad_defs + "".join(grid) + ev_layer + area + band
               + lines + dots + crit + "".join(xlab) + hov + "</svg>")
    if not interactive:
        return svg_out
    data = html.escape(json.dumps([[str(a), b] for a, b in points]), quote=True)
    est_attr = "" if estimated_until is None else str(estimated_until)
    return (f'<div class="linechart" data-points="{data}" data-est="{est_attr}" '
            f'data-color="{color}" data-w="{w}" data-h="{h}">{svg_out}</div>')


def bars(rows: list[tuple[str, float]], fmt=_money, color: str = PRIMARY,
         max_rows: int = 20) -> str:
    """Horizontal CSS bars for ranked lists (categories, merchants)."""
    rows = rows[:max_rows]
    if not rows:
        return '<div class="mut">No data.</div>'
    mx = max((abs(v) for _, v in rows), default=1) or 1
    out = ['<div class="barlist">']
    for label, val in rows:
        pct = 100 * abs(val) / mx
        c = RED if val < 0 else color
        out.append(
            f'<div class="barrow"><div class="barlbl">{html.escape(str(label))}</div>'
            f'<div class="bartrack"><div class="barfill" style="width:{pct:.1f}%;background:{c}"></div></div>'
            f'<div class="barval">{fmt(val)}</div></div>')
    out.append("</div>")
    return "".join(out)
