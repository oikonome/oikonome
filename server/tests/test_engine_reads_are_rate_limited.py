"""The Today, lens and calendar reads recompute the household's engine on
every call, as the reports do; like the reports they carry a per-address
budget, so a loop cannot hold the shared workers."""
import unittest

from oikonome.web import api


class EngineReadsAreRateLimited(unittest.TestCase):
    def test_each_engine_read_carries_a_limit(self):
        want = {"/today/full", "/today/glance", "/lens/month", "/lens/year",
                "/calendar"}
        seen = set()
        for r in api.router.routes:
            path = getattr(r, "path", "")
            hit = next((w for w in want if path.endswith(w)), None)
            if hit and "GET" in getattr(r, "methods", ()):
                deps = [d.call for d in r.dependant.dependencies]
                self.assertTrue(any("limit.<locals>" in getattr(
                    c, "__qualname__", "") for c in deps), path)
                seen.add(hit)
        self.assertEqual(seen, want)


if __name__ == "__main__":
    unittest.main()
