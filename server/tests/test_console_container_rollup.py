"""Admin-console container rollup: a completed one-shot
init job (the `migrate` container, Exited 0) must read as DONE, not "stopped"
— it is meant to run once and stop, and showing it as stopped would raise a
false alarm on every healthy box. A non-one-shot service that exited 0 is still "stopped";
a non-zero exit / unhealthy is still "bad" (red)."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from oikonome.web import adminconsole


class ContainerRollupTests(unittest.TestCase):
    def _rollup(self, containers):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "ops").mkdir()
            (Path(d) / "ops" / "host-health.json").write_text(
                json.dumps({"containers": containers}))
            prev = os.environ.get("OIKONOME_STATE_DIR")
            os.environ["OIKONOME_STATE_DIR"] = d
            try:
                groups = adminconsole._host_state()["container_groups"]
            finally:
                if prev is None:
                    os.environ.pop("OIKONOME_STATE_DIR", None)
                else:
                    os.environ["OIKONOME_STATE_DIR"] = prev
        return {g["project"]: g for g in groups}

    def test_migrate_one_shot_is_done_not_stopped(self):
        g = self._rollup([
            {"name": "oikonome-a-app-1", "state": "running",
             "status": "Up 2 hours (healthy)"},
            {"name": "oikonome-a-worker-1", "state": "running",
             "status": "Up 2 hours"},
            {"name": "oikonome-a-caddy-1", "state": "running",
             "status": "Up 2 hours"},
            {"name": "oikonome-a-redis-1", "state": "running",
             "status": "Up 6 hours"},
            {"name": "oikonome-a-migrate-1", "state": "exited",
             "status": "Exited (0) 2 hours ago"},
        ])["oikonome-a"]
        self.assertEqual(g["ok"], 4)
        self.assertEqual(g["done"], 1)        # the migrate one-shot
        self.assertEqual(g["stopped"], 0)     # NOT counted as stopped
        self.assertEqual(g["bad"], [])

    def test_non_oneshot_clean_exit_is_stopped(self):
        g = self._rollup([
            {"name": "oikonome-b-app-1", "state": "running",
             "status": "Up 1 hour (healthy)"},
            {"name": "oikonome-b-postgres-1", "state": "exited",
             "status": "Exited (0) 1 hour ago"},   # intentionally down
        ])["oikonome-b"]
        self.assertEqual(g["ok"], 1)
        self.assertEqual(g["done"], 0)
        self.assertEqual(g["stopped"], 1)
        self.assertEqual(g["bad"], [])

    def test_nonzero_exit_is_bad(self):
        g = self._rollup([
            {"name": "oikonome-a-app-1", "state": "exited",
             "status": "Exited (1) 5 minutes ago"},
            {"name": "oikonome-a-migrate-1", "state": "exited",
             "status": "Exited (0) 5 minutes ago"},
        ])["oikonome-a"]
        self.assertEqual(g["done"], 1)        # migrate still done
        self.assertEqual(len(g["bad"]), 1)    # the crashed app is red
        self.assertEqual(g["bad"][0]["name"], "oikonome-a-app-1")


if __name__ == "__main__":
    unittest.main()
