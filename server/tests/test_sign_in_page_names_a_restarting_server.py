"""The server-rendered sign-in says "restarting" when the proxy answered.

While the app is away (a deploy, a capacity step) the proxy in front of it
answers sign-ins with its own HTML page under a 502, 503 or 504. Read as
the app's words, that page made the sign-in say "the server is busy" (the
app's load-shedding 503) or "couldn't reach the server", neither of which
is what happened. The page's script tells the proxy's page from the app's
own answer the same way the app's client does — by content-type and by
whether the body opens like markup — and says the server is restarting,
with the Retry-After wait when one came.
"""

import json
import pathlib
import shutil
import subprocess
import unittest

import oikonome

_PAGES_JS = (pathlib.Path(oikonome.__file__).parent / "web" / "static"
             / "pages.js")
_HELPERS = ("inWait", "busyText", "restartingText", "appBody",
            "proxyAnswered")


def _function(src: str, name: str) -> str:
    """The source of one named function declaration, braces balanced
    (a brace inside a quoted string does not count)."""
    start = src.index(f"function {name}(")
    depth, i, quote = 0, src.index("{", start), None
    while True:
        ch = src[i]
        if quote:
            if ch == "\\":
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        else:
            depth += ch == "{"
            depth -= ch == "}"
        i += 1
        if depth == 0:
            return src[start:i]


_CASES = [
    # status, content-type, body, retry-after -> restarting?, sentence
    (503, "text/html; charset=utf-8", "<!doctype html><p>Back soon</p>",
     "30", True, "The server is restarting — try again in 30 seconds."),
    (502, "text/html", "<html>gateway</html>", None, True,
     "The server is restarting — try again in a moment."),
    (504, "", "", None, True,
     "The server is restarting — try again in a moment."),
    # an edge that rewrote the content-type still serves markup
    (502, "text/plain", "  <html>gateway</html>", "5", True,
     "The server is restarting — try again in 5 seconds."),
    # the app's own load-shedding 503 is JSON and stays "busy"
    (503, "application/json", '{"detail": "busy"}', "3", False,
     "The server is busy — try again in 3 seconds."),
    # the app's own 502 in plain words is not the proxy
    (502, "text/plain", "upstream bank error", None, False, None),
    (500, "text/html", "<html>oops</html>", None, False, None),
]


class SignInPageNamesARestartingServerTests(unittest.TestCase):
    def test_the_proxys_page_reads_as_a_restart(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        src = _PAGES_JS.read_text()
        helpers = "\n".join(_function(src, n) for n in _HELPERS)
        script = helpers + """
const cases = %s;
const failed = [];
for (const [status, ct, body, ra, restarting, text] of cases) {
  const got = proxyAnswered(status, ct, body);
  if (got !== restarting) failed.push(`${status} ${ct}: ${got}`);
  if (text !== null) {
    const said = restarting ? restartingText(ra) : busyText(ra);
    if (said !== text) failed.push(`${status}: ${said}`);
  }
}
if (failed.length) { console.error(failed.join("\\n")); process.exit(1); }
""" % json.dumps(_CASES)
        r = subprocess.run([node, "-e", script], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_the_restart_is_named_before_any_detail_is_read(self):
        """The proxy's markup must never reach the detail checks (a page
        that happened to contain a code word would be read as one) nor the
        "busy" and "couldn't reach" branches below them."""
        src = _PAGES_JS.read_text()
        flow = src[src.index('fetch("/api/login", {'):]
        self.assertIn("restarting: proxyAnswered(", flow)
        branch = flow.index("if (res.restarting)")
        self.assertLess(branch, flow.index('"human_check_failed"'))
        self.assertLess(branch, flow.index("res.status === 503"))
        self.assertIn("restartingText(res.retryAfter)",
                      flow[branch:branch + 200])


if __name__ == "__main__":
    unittest.main()
