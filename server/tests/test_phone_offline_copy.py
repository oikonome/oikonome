"""The phone's offline copy keeps the Month lens for the current month,
the Year lens for the current year, and Help, the way it keeps Today,
Bills and Accounts: in the encrypted persisted cache, shown offline under
"Offline — showing what was here at 9:41 AM.".

Before, in airplane mode the Month lens said "Couldn't load the month —
you're offline." and Help had nothing at all. (specs/screen-states.md)
"""

import pathlib
import unittest

import oikonome

_MOB = pathlib.Path(oikonome.__file__).parent.parent.parent / "mobile" / "src"


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


class PhoneOfflineCopy(unittest.TestCase):
    def test_the_persister_keeps_what_keptoffline_names(self):
        q = _src(self, _MOB / "lib" / "query.ts")
        self.assertIn("keptOffline(q.queryKey, offlineDay())", q)
        self.assertNotIn("const OFFLINE_KEYS", q)
        pure = _src(self, _MOB / "lib" / "pure.ts")
        fn = pure[pure.index("export function keptOffline("):]
        fn = fn[:fn.index("\n}\n")]
        self.assertIn('key[1] === "month" && key[2] === y && key[3] === m', fn)
        self.assertIn('key[1] === "year" && key[2] === y && key.length === 3', fn)
        keys = pure[pure.index("export const OFFLINE_KEYS"):]
        keys = keys[:keys.index("]);")]
        self.assertIn('"help"', keys)

    def test_the_kept_screens_say_their_copy_is_old(self):
        # the lens keys the screens ask with are the ones keptOffline keeps
        self.assertIn('queryKey: ["lens", "month", ym.y, ym.m]',
                      _src(self, _MOB / "app" / "month.tsx"))
        self.assertIn('queryKey: ["lens", "year", y]',
                      _src(self, _MOB / "app" / "year.tsx"))
        self.assertIn('queryKey: ["help"]', _src(self, _MOB / "app" / "help.tsx"))
        for f in ("month.tsx", "year.tsx", "help.tsx"):
            self.assertIn("<StaleLine query={query} />",
                          _src(self, _MOB / "app" / f), f)


if __name__ == "__main__":
    unittest.main()
