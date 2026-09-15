#!/usr/bin/env python3
"""Render the PWA / home-screen icons from the SVG masters.

Always render the mark from SVG — never hand-draw the geometry in PIL. The
arc, its stroke width and its optical centring live in one place
(webapp/public/icon.svg and icon-maskable.svg); a second, hand-rolled copy
drifts the moment either changes.

  scripts/gen-app-icons.py           # write the PNGs
  scripts/gen-app-icons.py --check   # verify they match the SVGs (CI-safe)

Outputs, all into webapp/public/:

  icon-192.png            Android home screen / launcher ("any")
  icon-512.png            splash + store-quality "any"
  icon-maskable-512.png   full-bleed, for the platform's own mask
  apple-touch-icon.png    iOS home screen — 180px, opaque, no rounding of
                          our own (iOS applies the squircle itself)

iOS ignores SVG icons entirely, which is why the PNGs have to exist:
without them an iPhone home-screen install falls back to a page screenshot.
"""
from __future__ import annotations

import argparse
import io
import pathlib
import sys

import cairosvg

PUBLIC = pathlib.Path(__file__).resolve().parent.parent / "webapp" / "public"

# (source svg, output png, pixel size)
RENDERS = [
    ("icon.svg", "icon-192.png", 192),
    ("icon.svg", "icon-512.png", 512),
    ("icon-maskable.svg", "icon-maskable-512.png", 512),
    # Apple's icon must be opaque and unrounded; the maskable master is
    # exactly that, so it doubles as the source here.
    ("icon-maskable.svg", "apple-touch-icon.png", 180),
]


def render(src: str, size: int) -> bytes:
    return cairosvg.svg2png(
        url=str(PUBLIC / src), output_width=size, output_height=size)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="fail if any PNG is missing or stale")
    args = ap.parse_args()

    stale = []
    for src, out, size in RENDERS:
        png = render(src, size)
        dest = PUBLIC / out
        if args.check:
            if not dest.exists() or dest.read_bytes() != png:
                stale.append(out)
            continue
        dest.write_bytes(png)
        print(f"{out}  {size}×{size}  {len(png):,} bytes  ← {src}")

    if args.check:
        if stale:
            print("stale or missing: " + ", ".join(stale), file=sys.stderr)
            print("re-run scripts/gen-app-icons.py", file=sys.stderr)
            return 1
        print(f"ok: {len(RENDERS)} icons match their SVG masters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
