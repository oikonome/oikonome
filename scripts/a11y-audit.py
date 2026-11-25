#!/usr/bin/env python3
"""Accessibility measurement of the web app against a running instance.

Runs axe-core (WCAG 2.0/2.1/2.2 A + AA rules, plus best-practice) on each
page in both colour themes, walks the first Tab stops of each page to see
whether every stop has a name and a visible focus indicator, and checks
reflow at 320 CSS px (WCAG 1.4.10). Requirements: specs/accessibility.md.

    OIKONOME_SESSION=<session token> server/.venv/bin/python scripts/a11y-audit.py \\
        --base http://127.0.0.1:8042 --axe <path to axe.min.js> [--dist webapp/dist] \\
        [--pages /app/,/app/bills] [--out report.json]

  --axe   axe-core's axe.min.js. It is not a project dependency: install it
          wherever you like (`npm install axe-core@4.10.2` in a scratch dir).
  --dist  serve the SPA from a local build instead of the instance's own,
          so a tree can be measured before it is deployed (the API still
          comes from --base).
  OIKONOME_SESSION  the value of an `oikonome_session` cookie for a signed-in
          user. Without it only the signed-out pages are measured.

Read-only by construction: every non-GET request to /api/ is aborted and
listed in the report, so a page that writes on load shows up instead of
writing. The report carries rule ids, impacts, counts and CSS selectors —
never element text, so the figures on screen stay out of it.
"""
import argparse, json, mimetypes, os, pathlib, re, sys, time

SIGNED_OUT = ["/login"]
SIGNED_IN = [
    "/app/", "/app/?lens=today", "/app/?lens=month", "/app/?lens=year", "/app/transactions",
    "/app/accounts", "/app/bills", "/app/budget", "/app/cashflow?tab=spending",
    "/app/cashflow", "/app/networth", "/app/business", "/app/settings",
    "/app/reimburse", "/app/welcome",
]
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]

# For each of the first N Tab stops: a short selector, whether it has an
# accessible name (any of the usual sources — the text itself is not
# returned), and whether the focused element paints an outline or a ring.
FOCUS_JS = """() => {
  const el = document.activeElement;
  if (!el || el === document.body) return null;
  const sel = (n) => { let s = n.tagName.toLowerCase();
    if (n.id) return s + '#' + n.id;
    const c = (n.className && typeof n.className === 'string')
      ? n.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
    return c ? s + '.' + c : s; };
  const named = !!(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby')
    || el.getAttribute('title') || (el.labels && el.labels.length)
    || (el.innerText || '').trim() || el.querySelector('img[alt]:not([alt=""]), svg title'));
  const cs = getComputedStyle(el);
  const ring = (cs.outlineStyle !== 'none' && parseFloat(cs.outlineWidth) > 0)
    || (cs.boxShadow && cs.boxShadow !== 'none');
  return {named, ring,
          path: el.parentElement ? sel(el.parentElement) + ' > ' + sel(el) : sel(el)};
}"""

REFLOW_JS = """() => {
  const W = document.documentElement.clientWidth;
  const over = document.documentElement.scrollWidth - W;
  const out = [];
  if (over > 1) {
    for (const n of document.querySelectorAll('body *')) {
      const r = n.getBoundingClientRect();
      if (r.right > W + 1 && r.width > 0) {
        // skip children of an element that scrolls or clips on its own
        let p = n.parentElement, inside = false;
        while (p && p !== document.body) {
          const o = getComputedStyle(p).overflowX;
          if (o === 'auto' || o === 'scroll' || o === 'hidden') { inside = true; break; }
          p = p.parentElement; }
        if (!inside) { const c = (typeof n.className === 'string' && n.className.trim())
            ? '.' + n.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
          out.push(n.tagName.toLowerCase() + c); }
        if (out.length >= 8) break; } } }
  return {overflow_px: Math.max(0, over), widest: out};
}"""


def clean(selector):
    """A selector without the data in it: attribute values (a title can
    carry a figure, a value an amount) and data-derived ids are dropped."""
    s = re.sub(r'\[([\w-]+)([~|^$*]?=)"[^"]*"\]', r"[\1]", selector)
    return re.sub(r"#((?:[^\s>.:\[\\]|\\.)+)",
                  lambda m: "#" + m.group(1) if re.fullmatch(r"[a-z]+", m.group(1))
                  else "#" + re.split(r"[-_\\]", m.group(1))[0] + "-…", s)


def contrast(node):
    """fg/bg colours and the ratio axe measured — colours, never text."""
    for chk in node.get("any", []):
        d = chk.get("data") or {}
        if isinstance(d, dict) and "contrastRatio" in d:
            return {"fg": d.get("fgColor"), "bg": d.get("bgColor"),
                    "ratio": d.get("contrastRatio"), "need": d.get("expectedContrastRatio")}
    return None


def settle(pg, inflight, quiet_ms=700, cap_s=20):
    """Wait until no request has been in flight for quiet_ms (or cap_s)."""
    end, quiet_since = time.time() + cap_s, None
    while time.time() < end:
        if inflight[0] <= 0:
            quiet_since = quiet_since or time.time()
            if (time.time() - quiet_since) * 1000 >= quiet_ms:
                return
        else:
            quiet_since = None
        pg.wait_for_timeout(100)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8042")
    ap.add_argument("--axe", required=True)
    ap.add_argument("--dist")
    ap.add_argument("--pages")
    ap.add_argument("--tabs", type=int, default=30)
    ap.add_argument("--out")
    a = ap.parse_args()
    axe_src = pathlib.Path(a.axe).read_text()
    token = os.environ.get("OIKONOME_SESSION", "")
    targets = ([p for p in a.pages.split(",") if p] if a.pages
               else SIGNED_OUT + (SIGNED_IN if token else []))
    dist = pathlib.Path(a.dist).resolve() if a.dist else None
    host = a.base.split("//", 1)[1].split(":")[0].split("/")[0]
    blocked, report = [], {"base": a.base, "pages": {}}

    from playwright.sync_api import sync_playwright

    def route(r):
        req, url = r.request, r.request.url
        rest = "/" + url.split("//", 1)[1].split("/", 1)[-1]
        if "/api/" in rest and req.method not in ("GET", "HEAD", "OPTIONS"):
            blocked.append(f"{req.method} {rest.split('?')[0]}")
            return r.abort()
        if dist and rest.startswith("/app/") and "/api/" not in rest:
            rel = rest[len("/app/"):].split("?")[0].split("#")[0]
            f = dist / rel if rel else dist / "index.html"
            if not f.is_file():
                f = dist / "index.html"   # client routes get the shell
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            return r.fulfill(status=200, body=f.read_bytes(), content_type=ctype)
        return r.continue_()

    with sync_playwright() as p:
        b = p.chromium.launch()
        try:
            for scheme in ("dark", "light"):
                ctx = b.new_context(color_scheme=scheme, bypass_csp=True,
                                    viewport={"width": 1280, "height": 900},
                                    reduced_motion="reduce")
                ctx.route("**/*", route)
                for target in targets:
                    ctx.clear_cookies()
                    if target.startswith("/app") and token:
                        ctx.add_cookies([{"name": "oikonome_session", "value": token,
                                          "domain": host, "path": "/"}])
                    pg = ctx.new_page()
                    inflight = [0]
                    pg.on("request", lambda _r: inflight.__setitem__(0, inflight[0] + 1))
                    for ev in ("requestfinished", "requestfailed"):
                        pg.on(ev, lambda _r: inflight.__setitem__(0, inflight[0] - 1))
                    pg.goto(a.base + target, wait_until="load")
                    settle(pg, inflight)
                    pg.add_script_tag(content=axe_src)
                    res = pg.evaluate("""(tags) => axe.run(document,
                        {runOnly: {type: 'tag', values: tags}, resultTypes: ['violations']})""", TAGS)
                    v = [{"id": x["id"], "impact": x["impact"],
                          "tags": [t for t in x["tags"] if t.startswith("wcag") or t == "best-practice"],
                          "nodes": len(x["nodes"]),
                          "targets": [clean(" ".join(map(str, n["target"]))) for n in x["nodes"][:6]],
                          **({"colours": sorted({json.dumps(c) for c in map(contrast, x["nodes"]) if c})}
                             if x["id"] == "color-contrast" else {})}
                         for x in res["violations"]]
                    entry = report["pages"].setdefault(target, {})
                    entry[scheme] = {"final_url": pg.url.replace(a.base, ""), "axe": v}
                    if scheme == "dark":
                        stops = []
                        pg.mouse.click(1, 1)
                        for _ in range(a.tabs):
                            pg.keyboard.press("Tab")
                            s = pg.evaluate(FOCUS_JS)
                            if s: stops.append(s)
                        entry["focus"] = {
                            "stops": len(stops),
                            "unnamed": sorted({s["path"] for s in stops if not s["named"]}),
                            "no_ring": sorted({s["path"] for s in stops if not s["ring"]}),
                        }
                        pg.set_viewport_size({"width": 320, "height": 800})
                        pg.wait_for_timeout(400)
                        entry["reflow_320"] = pg.evaluate(REFLOW_JS)
                    pg.close()
                    n = sum(x["nodes"] for x in v)
                    print(f"{scheme:5s} {target:28s} {len(v):2d} rules {n:4d} nodes", flush=True)
                ctx.close()
        finally:
            b.close()
    report["blocked_writes"] = sorted(set(blocked))
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(report, indent=1))
    print("blocked writes:", report["blocked_writes"] or "none")


if __name__ == "__main__":
    sys.exit(main())
