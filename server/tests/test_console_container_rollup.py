"""Admin-console container rollup: a completed one-shot
init job (the `migrate` container, Exited 0) must read as DONE, not "stopped"
— it's supposed to run once and stop, and showing it as stopped alarmed on
every healthy box. A non-one-shot service that exited 0 is still "stopped";
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
            {"name": "oikonome-8042-app-1", "state": "running",
             "status": "Up 2 hours (healthy)"},
            {"name": "oikonome-8042-worker-1", "state": "running",
             "status": "Up 2 hours"},
            {"name": "oikonome-8042-caddy-1", "state": "running",
             "status": "Up 2 hours"},
            {"name": "oikonome-8042-redis-1", "state": "running",
             "status": "Up 6 hours"},
            {"name": "oikonome-8042-migrate-1", "state": "exited",
             "status": "Exited (0) 2 hours ago"},
        ])["oikonome-8042"]
        self.assertEqual(g["ok"], 4)
        self.assertEqual(g["done"], 1)        # the migrate one-shot
        self.assertEqual(g["stopped"], 0)     # NOT counted as stopped
        self.assertEqual(g["bad"], [])

    def test_non_oneshot_clean_exit_is_stopped(self):
        g = self._rollup([
            {"name": "oikonome-8043-app-1", "state": "running",
             "status": "Up 1 hour (healthy)"},
            {"name": "oikonome-8043-postgres-1", "state": "exited",
             "status": "Exited (0) 1 hour ago"},   # intentionally down
        ])["oikonome-8043"]
        self.assertEqual(g["ok"], 1)
        self.assertEqual(g["done"], 0)
        self.assertEqual(g["stopped"], 1)
        self.assertEqual(g["bad"], [])

    def test_nonzero_exit_is_bad(self):
        g = self._rollup([
            {"name": "oikonome-8042-app-1", "state": "exited",
             "status": "Exited (1) 5 minutes ago"},
            {"name": "oikonome-8042-migrate-1", "state": "exited",
             "status": "Exited (0) 5 minutes ago"},
        ])["oikonome-8042"]
        self.assertEqual(g["done"], 1)        # migrate still done
        self.assertEqual(len(g["bad"]), 1)    # the crashed app is red
        self.assertEqual(g["bad"][0]["name"], "oikonome-8042-app-1")


if __name__ == "__main__":
    unittest.main()
