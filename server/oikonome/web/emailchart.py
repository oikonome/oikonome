"""The cash chart as a PNG, for the daily email.

Mail clients strip SVG, so the chart the Today page draws — the
autopay-statement balance over its shaded area, the dashed pay-all-cards-now
line, a dashed mark on each card's autopay day, the red $0 line and the
critical-date marker — is drawn again here with Pillow, on the same geometry
as `charts.line` / the SPA's static AreaChart, and travels inside the message
as an inline image part (report.send turns the data: URI into one).

Pillow draws no anti-aliased lines, so the picture is drawn at 4x and reduced
to 2x: the delivered image is a retina rendering of the 720x220 chart.
"""
from __future__ import annotations

import io
import math

from PIL import Image, ImageDraw, ImageFont

W, H = 720, 220                       # the chart's CSS box, as the app's viewBox
PAD_L, PAD_R, PAD_T, PAD_B = 64, 12, 12, 26
SS, OUT = 4, 2                        # drawing density → delivered density

# the email's dark palette (the card the chart sits on is #1a212b)
BG = (26, 33, 43)
BLUE, AMBER, RED = (79, 155, 214), (240, 180, 41), (224, 105, 93)
MUT, GRID = (139, 152, 163), (42, 50, 61)


def _mix(c, a):
    """`c` at opacity `a` over the card — solid colours survive every
    renderer, alpha does not."""
    return tuple(round(ci * a + bi * (1 - a)) for ci, bi in zip(c, BG))


def _short_money(v: float) -> str:
    sign, a = ("-" if v < 0 else ""), abs(v)
    if a >= 1e6:
        return sign + "$" + f"{a / 1e6:.2f}".rstrip("0").rstrip(".") + "M"
    if a >= 1e3:
        return sign + "$" + f"{round(a / 1e3):,}k"
    return sign + "$" + f"{round(a):,}"


def _font(px: float):
    return ImageFont.load_default(size=round(px * SS))


def _dashed(draw, pts, dash, gap, fill, width):
    """Stroke a polyline with a dash pattern that runs on across its
    vertices, the way an SVG stroke-dasharray does."""
    on, left = True, dash
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        seg = math.hypot(x1 - x0, y1 - y0)
        pos = 0.0
        while seg and pos < seg:
            step = min(left, seg - pos)
            if on:
                t0, t1 = pos / seg, (pos + step) / seg
                draw.line([(x0 + (x1 - x0) * t0, y0 + (y1 - y0) * t0),
                           (x0 + (x1 - x0) * t1, y0 + (y1 - y0) * t1)],
                          fill=fill, width=width)
            pos += step
            left -= step
            if left <= 1e-9:
                on = not on
                left = dash if on else gap


def _vtext(img, x, y, text, font, fill):
    """Text running UP from its baseline start at (x, y), glyph tops to the
    left — SVG's rotate(-90) about the anchor."""
    asc, desc = font.getmetrics()
    # never taller than the plot: a long combined autopay name ran off the top
    room = (H - PAD_T - PAD_B - 8) * SS
    if font.getlength(text) > room:
        while text and font.getlength(text + "…") > room:
            text = text[:-1]
        text += "…"
    tw = max(1, round(font.getlength(text)))
    tile = Image.new("RGBA", (tw, asc + desc), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((0, asc), text, font=font, fill=fill, anchor="ls")
    tile = tile.rotate(90, expand=True)
    img.paste(tile, (round(x - asc), round(y - tw)), tile)


def render(points: list[tuple[str, float]],
           second: list[tuple[str, float]] | None = None,
           events: list[tuple[int, str]] | None = None,
           mark_x: int | None = None, mark_label: str = "",
           labels: tuple = ("autopay statement balance", "pay all cards now"),
           ) -> bytes:
    """PNG bytes of the cash chart; b"" when there is nothing to draw."""
    n = len(points)
    if not n:
        return b""
    s = SS
    sec = list(second or [])[:n]
    ys = [float(v) for _, v in points] + [float(v) for _, v in sec]
    ymin, ymax = min(ys + [0.0]), max(ys + [0.0])
    span = (ymax - ymin) or 1.0
    iw, ih = W - PAD_L - PAD_R, H - PAD_T - PAD_B

    def X(i):
        return (PAD_L + iw * i / max(1, n - 1)) * s

    def Y(v):
        return (PAD_T + ih - ih * (float(v) - ymin) / span) * s

    pts = [(X(i), Y(v)) for i, (_, v) in enumerate(points)]
    img = Image.new("RGB", (W * s, H * s), BG)
    d = ImageDraw.Draw(img)
    # zero-anchored area, 12% blue
    d.polygon([(X(0), Y(0))] + pts + [(X(n - 1), Y(0))], fill=_mix(BLUE, 0.12))
    f9, f10, f11 = _font(9), _font(10), _font(11)
    # min / mid / max / 0 gridlines, labelled on the left — $0 first, and a
    # tick that would print on top of one already drawn is skipped (a
    # midpoint near zero printed "-$0" over "$0")
    drawn: list[float] = []
    for gv in [0.0, ymax, ymin, (ymin + ymax) / 2]:
        y = Y(gv)
        if any(abs(y - y2) < 14 * s for y2 in drawn):
            continue
        drawn.append(y)
        d.line([(PAD_L * s, y), ((W - PAD_R) * s, y)], fill=GRID, width=s)
        d.text(((PAD_L - 6) * s, y + 3 * s), _short_money(gv), font=f11,
               fill=MUT, anchor="rs")
    # card autopays: behind the series, the label running up the line
    for i, lab in events or []:
        if not 0 <= i < n:
            continue
        x = X(i)
        _dashed(d, [(x, PAD_T * s), (x, (PAD_T + ih) * s)], 3 * s, 3 * s,
                _mix(MUT, 0.75), s)
        _vtext(img, x - 3 * s, (PAD_T + ih - 4) * s, lab, f9, MUT)
    d.line(pts, fill=BLUE, width=round(2.5 * s), joint="curve")
    if sec:
        _dashed(d, [(X(i), Y(v)) for i, (_, v) in enumerate(sec)],
                6 * s, 4 * s, AMBER, 2 * s)
        # legend, top right
        lx0 = W - PAD_R - 205
        for k, (col, dashed, lab) in enumerate(
                [(BLUE, False, labels[0]), (AMBER, True, labels[1])]):
            y = (PAD_T + 8 + k * 14) * s
            seg = [(lx0 * s, y), ((lx0 + 18) * s, y)]
            if dashed:
                _dashed(d, seg, 6 * s, 4 * s, col, 2 * s)
            else:
                d.line(seg, fill=col, width=round(2.5 * s))
            d.text(((lx0 + 23) * s, (PAD_T + 11 + k * 14) * s), lab, font=f10,
                   fill=MUT, anchor="ls")
    # $0 — out of money
    y0 = Y(0)
    _dashed(d, [(PAD_L * s, y0), ((W - PAD_R) * s, y0)], 5 * s, 3 * s,
            _mix(RED, 0.85), round(1.5 * s))
    d.text(((W - PAD_R) * s, y0 - 4 * s), "$0 · out of money", font=f10,
           fill=RED, anchor="rs")
    # the critical date
    if mark_x is not None and 0 <= mark_x < n:
        x = X(mark_x)
        _dashed(d, [(x, PAD_T * s), (x, (PAD_T + ih) * s)], 5 * s, 3 * s,
                RED, round(1.5 * s))
        cy, r = Y(points[mark_x][1]), 4.5 * s
        d.ellipse([x - r, cy - r, x + r, cy + r], fill=RED)
        if mark_label:
            left = mark_x < n / 2
            d.text((x + (6 if left else -6) * s, (PAD_T + 11) * s), mark_label,
                   font=f11, fill=RED, anchor="ls" if left else "rs")
    # x axis: a handful of dates, as the app prints them
    for i in range(0, n, max(1, n // 6)):
        d.text((X(i), (H - 8) * s), str(points[i][0]), font=f11, fill=MUT,
               anchor="ms")
    out = img.resize((W * OUT, H * OUT), Image.LANCZOS)
    # a palette PNG: a few flat colours plus their anti-aliased edges
    out = out.quantize(colors=128, method=Image.Quantize.MEDIANCUT, dither=0)
    buf = io.BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()
