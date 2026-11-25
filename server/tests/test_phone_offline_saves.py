"""Offline, the phone's write buttons are greyed with "You're offline."
beside them, instead of spinning and then failing.

The connection is read from what the app already knows — a request that
got no answer at all (the shared failed-read cause), and a look at the
server's health door on every return to the app — through one gate
(lib/connection.ts, components/offline.tsx) that the save buttons read.
No native network module was added. (specs/screen-states.md)
"""

import json
import pathlib
import unittest

import oikonome

_APP = pathlib.Path(oikonome.__file__).parent.parent.parent
_MOB = _APP / "mobile"


def _src(test: unittest.TestCase, p: pathlib.Path) -> str:
    if not p.exists():
        test.skipTest(f"{p} not present")
    return p.read_text()


class PhoneOfflineSaves(unittest.TestCase):
    def test_the_gate_follows_the_apps_own_requests(self):
        q = _src(self, _MOB / "src" / "lib" / "query.ts")
        self.assertIn("bindConnection(queryClient);", q)
        self.assertIn("resetConnection();", q)
        conn = _src(self, _MOB / "src" / "lib" / "connection.ts")
        self.assertIn("offlineAfter(offline, e)", conn)
        self.assertIn('baseUrl + "/healthz"', conn)
        root = _src(self, _MOB / "src" / "app" / "_layout.tsx")
        self.assertIn("<ConnectionWatch />", root)
        gate = _src(self, _MOB / "src" / "components" / "offline.tsx")
        self.assertIn("export function useOffline(): boolean", gate)
        self.assertIn("{OFFLINE_WRITE}", gate)

    def test_no_native_network_module(self):
        pkg = json.loads(_src(self, _MOB / "package.json"))
        deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
        self.assertNotIn("expo-network", deps)
        self.assertNotIn("@react-native-community/netinfo", deps)

    def test_the_main_write_surfaces_read_the_one_gate(self):
        for rel in ("bill.tsx", "budget.tsx", "txn.tsx", "settings.tsx",
                    "reimburse.tsx", "(tabs)/bills.tsx"):
            src = _src(self, _MOB / "src" / "app" / rel)
            self.assertIn("useOffline()", src, rel)
            self.assertIn("<OfflineNote", src, rel)
            self.assertNotIn("NetInfo", src, rel)


if __name__ == "__main__":
    unittest.main()
