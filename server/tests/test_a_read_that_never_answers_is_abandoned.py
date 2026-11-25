"""A read the server or the network never answers ends on its own.

Without a limit a GET that gets no response leaves its screen on
"Loading…" with no way out but a reload. Each client has one function
every request passes through, and that function hands a read to
fetchRead (the screen-states twin block; its behaviour is pinned by the
case table in mobile/scripts/money-cases.mjs, run against both clients):
a read with no answer inside the limit fails as a request that got no
answer does, a write is never timed, and the caller's signal still
cancels it (specs/screen-states.md, "A read that never answers").

The checks here pin each client to that one function: a fetch that goes
round it waits forever again, and a read abandoned at the limit that is
reported as an abort would sign the web out.
"""

import pathlib
import re
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_WEB = _APP / "webapp" / "src"
_MOB = _APP / "mobile" / "src"

_HEAD = "// ---- screen states: twin block ----"
_TAIL = "// ---- end of the screen-states twin block ----"


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


def _fn(src: str, head: str) -> str:
    """The text of the function that starts at `head`, to its closing
    brace at the left margin."""
    start = src.index(head)
    return src[start:src.index("\n}\n", start)]


class ReadsAreLimited(unittest.TestCase):
    def test_the_limit_is_one_function_in_the_shared_block(self):
        for p in (_WEB / "moneymath.ts", _MOB / "lib" / "pure.ts"):
            src = _src(self, p)
            block = src[src.index(_HEAD):src.index(_TAIL)]
            self.assertIn("export const READ_LIMIT_MS = 30_000;", block, p)
            for name in ("isRead", "timedOut", "readFinished"):
                self.assertIn(f"export function {name}(", block, p)
            self.assertIn("export async function fetchRead<", block, p)
            fn = _fn(block, "export async function fetchRead<")
            # a write goes out with the caller's signal and no clock
            self.assertIn(
                "if (!isRead(method)) return send(outer ?? undefined);", fn, p)
            # what a timed-out read throws reads as "no answer": the
            # failed line, the stale line and the phone's offline gate all
            # key on a TypeError with no status
            self.assertIn('new TypeError("no answer in time")', fn, p)
            # the engines the phone runs on may not have it
            self.assertNotIn("AbortSignal.any(", block, p)
            self.assertNotIn("AbortSignal.timeout(", block, p)

    def test_a_timed_out_read_is_asked_again_once(self):
        """Three automatic retries of a 30-second wait is two silent
        minutes; one, then the failed line and its Try again."""
        for p in (_WEB / "moneymath.ts", _MOB / "lib" / "pure.ts"):
            fn = _fn(_src(self, p), "export function retryRead(")
            self.assertIn("if (timedOut(e)) return failures < 1;", fn, p)

    def test_every_web_request_passes_the_limit(self):
        src = _src(self, _WEB / "api" / "client.ts")
        fn = _fn(src, "async function rawFetch(")
        self.assertIn("fetchRead(go, init?.method, outer)", fn)
        self.assertIn("init?.signal ?? epoch.signal", fn)
        # the only bare fetch in the module is the one rawFetch wraps
        self.assertEqual(len(re.findall(r"(?<![\w.])fetch\(", src)), 1)
        self.assertEqual(len(re.findall(r"(?<![\w.])fetch\(", fn)), 1)
        # only an abort is read as a sign-out; the abandoned read is a
        # TypeError and falls through to the caller as it is
        self.assertIn('?.name === "AbortError")', fn)
        self.assertNotIn("timedOut", fn)
        # the one read let off the limit is the file download
        self.assertEqual(src.count('"unlimited")'), 1)
        self.assertIn('rawFetch(path, { credentials: "same-origin" }, "unlimited")',
                      src[src.index("  saveFile: async ("):])
        # an answered read lets go of the epoch once its body is read
        self.assertIn("finally { readFinished(r); }", _fn(src, "async function settle<"))

    def test_every_phone_request_passes_the_limit(self):
        conn = _src(self, _MOB / "lib" / "connection.ts")
        fn = _fn(conn, "export async function fetchStamped(")
        self.assertIn("fetchRead((signal) => fetchFn(input, { ...init, signal }),", fn)
        self.assertIn("init?.method, init?.signal);", fn)
        # the abandoned read is stamped like any other failed attempt, so
        # the offline gate weighs it against later answers
        self.assertIn("throw markAttempt(e, started);", fn)
        api = _src(self, _MOB / "lib" / "api.ts")
        # the api client never calls fetch itself
        self.assertEqual(re.findall(r"(?<![\w.])fetch\(", api), [])
        send = api[api.index("const send = async <T>("):]
        send = send[:send.index("\n  };\n")]
        self.assertIn("await fetchStamped(baseUrl + path, {", send)
        self.assertIn("readFinished(r);", send)


if __name__ == "__main__":
    unittest.main()
