"""Both clients say the same thing for a read that failed, and handle it
the same way.

The words (failLine, staleLine), the retry rule (retryRead) and the figure
gate (knownOr) are one twin block, kept character-for-character equal in
webapp/src/moneymath.ts and mobile/src/lib/pure.ts and pinned by the case
table in mobile/scripts/money-cases.mjs, which test_client_money_twins runs
against both (specs/screen-states.md). The checks here pin the clients to
that block: a phone that says "Couldn't reach the server" or "Offline" for
a reply that came back (a 5xx, a 403, a 429) tells the person the wrong
thing to fix; a phone that retries a 429 earns the address ban; a web page
whose own read gets a 401 sits on its error text instead of signing out.
"""

import pathlib
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


class ScreenStatesShared(unittest.TestCase):
    def test_the_screen_states_block_is_the_same_text_on_both(self):
        blocks = []
        for p in (_WEB / "moneymath.ts", _MOB / "lib" / "pure.ts"):
            src = _src(self, p)
            self.assertEqual(src.count(_HEAD), 1, p)
            blocks.append(src[src.index(_HEAD):src.index(_TAIL)])
        self.assertEqual(blocks[0], blocks[1])
        for name in ("failLine", "staleLine", "retryRead", "knownOr",
                     "busyText", "restartingText"):
            self.assertIn(f"export function {name}", blocks[0], name)

    def test_the_busy_words_are_written_once_per_client(self):
        """The sign-in page and the failed-read line say the same busy and
        restarting sentences because both read the one copy."""
        for p in (_WEB / "api" / "client.ts", _MOB / "lib" / "api.ts"):
            src = _src(self, p)
            self.assertNotIn("export function busyText", src, p)
            self.assertNotIn("function inWait", src, p)
            self.assertIn("export { busyText, restartingText };", src, p)

    def test_both_clients_ask_a_failed_read_again_by_one_rule(self):
        web = _src(self, _WEB / "main.tsx")
        self.assertIn("retry: retryRead,", web)
        self.assertIn("retryDelay: retryReadDelay,", web)
        mob = _src(self, _MOB / "lib" / "query.ts")
        self.assertIn("retry: retryRead,", mob)
        self.assertIn("retryDelay: retryReadDelay,", mob)
        self.assertNotRegex(mob, r"retry:\s*1\b")
        # the web's AuthError carries its status, so the rule and the
        # words read a 401 from the same field as the phone's ApiError
        client = _src(self, _WEB / "api" / "client.ts")
        self.assertRegex(client, r"class AuthError extends Error \{\s*\n"
                                 r"(?:\s*/\*\*.*\*/\s*\n)?\s*readonly status = 401;")

    def test_a_read_that_loses_the_session_signs_out_on_the_web(self):
        src = _src(self, _WEB / "main.tsx")
        cache = src[src.index("queryCache: new QueryCache("):]
        cache = cache[:cache.index("mutationCache:")]
        self.assertIn("readLostSession(err, query.queryKey)", cache)
        self.assertIn('new Event("oiko-auth-lost")', cache)
        fn = src[src.index("function readLostSession("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn("isReauthReply(err.message)", fn)
        # not the me read (App shows sign-in from it), and not once the
        # sign-out has cleared the cache: otherwise every read in flight
        # starts another one
        self.assertIn('key[0] === "me"', fn)
        self.assertIn('qc.getQueryData(["me"]) !== undefined', fn)

    def test_the_phone_never_blames_the_connection_for_an_answer(self):
        for tree in (_MOB / "app", _MOB / "components"):
            for p in sorted(tree.rglob("*.tsx")):
                src = p.read_text()
                self.assertNotRegex(src, r"Couldn(?:'|&apos;)t reach the server",
                                    p)
                self.assertNotIn("Offline — showing data from", src, p)
                self.assertNotIn("StaleBanner", src, p)
        self.assertFalse((_MOB / "components" / "stale-banner.tsx").exists())

    def test_the_shared_pieces_speak_through_the_block(self):
        for p, who in ((_WEB / "components" / "Loaded.tsx", '"web"'),
                       (_MOB / "components" / "loaded.tsx", '"phone"')):
            src = _src(self, p)
            for name in ("export function Loaded<D>", "export function FailedLine",
                         "export function StaleLine"):
                self.assertIn(name, src, p)
            self.assertIn(f"failLine(q.error, what, {who})", src, p)
            self.assertIn(f"staleLine(query.error, query.dataUpdatedAt, {who},", src, p)
            self.assertIn("inputPhase(q)", src, p)
        self.assertIn("export function EmptyState",
                      _src(self, _MOB / "components" / "loaded.tsx"))

    def test_the_phone_prints_the_unknown_mark_for_a_missing_figure(self):
        src = _src(self, _MOB / "lib" / "pure.ts")
        self.assertIn("export const money = (n: number | null | undefined,", src)
        self.assertIn("if (n === null || n === undefined) return UNKNOWN_FIGURE;",
                      src)


if __name__ == "__main__":
    unittest.main()
