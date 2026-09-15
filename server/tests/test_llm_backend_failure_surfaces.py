"""A model backend that fails every call must not fail silently.

The nightly LLM run records its own verdict; a run that produced nothing
because the backend failed (a 404 — the model is not loaded — an
unreachable host, a timeout) raises an alert on the Today strip and the
daily email, and a red row on the Doctor page, until a later run
produces again. Per-batch warnings in the app log are not a signal
anyone reads at 4am.
"""

import os
import unittest
from unittest.mock import patch

import httpx

from oikonome.engine import alerts, llm_categorize
from oikonome.web import doctor

from .test_llm_categorize import ENV, add_raw_txn, reply_transport
from .util import make_db


def status_transport(code: int):
    def handler(request):
        return httpx.Response(code, json={"error": "model not found"})
    return httpx.MockTransport(handler)


def dead_transport():
    def handler(request):
        raise httpx.ConnectError("refused")
    return httpx.MockTransport(handler)


class BackendFailureTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        add_raw_txn(self.conn, "t1", "2025-07-01", 42, "Mystery Shop", raw={})

    def tearDown(self):
        self.conn.close()

    def _run(self, transport):
        with patch.dict(os.environ, ENV):
            return llm_categorize.run(self.conn, transport=transport)

    def test_a_404_names_the_missing_model(self):
        stats = self._run(status_transport(404))
        self.assertEqual(stats["merchants_classified"], 0)
        self.assertIn("404", stats["backend_error"])
        self.assertIn("'test-model' is not loaded", stats["backend_error"])
        last = llm_categorize.last_run(self.conn)
        self.assertEqual(last["state"], "error")
        self.assertEqual(last["error"], stats["backend_error"])
        self.assertEqual(last["model"], "test-model")
        self.assertEqual(last["produced"], 0)

    def test_an_unreachable_backend_is_an_error_too(self):
        self._run(dead_transport())
        last = llm_categorize.last_run(self.conn)
        self.assertEqual(last["state"], "error")
        self.assertIn("unreachable", last["error"])

    def test_a_producing_run_records_done_and_clears_the_alert(self):
        self._run(status_transport(404))
        self.assertIsNotNone(alerts.llm_failing(self.conn))
        add_raw_txn(self.conn, "t2", "2025-07-02", 7, "Other Shop", raw={})
        self._run(reply_transport({"1": "FOOD_AND_DRINK",
                                   "2": "FOOD_AND_DRINK"}))
        last = llm_categorize.last_run(self.conn)
        self.assertEqual(last["state"], "done")
        self.assertIsNone(last["error"])
        self.assertGreater(last["produced"], 0)
        self.assertIsNone(alerts.llm_failing(self.conn))

    def test_no_backend_records_nothing(self):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("OIKONOME_LLM")}
        with patch.dict(os.environ, env, clear=True):
            llm_categorize.run(self.conn)
        self.assertIsNone(llm_categorize.last_run(self.conn))
        self.assertIsNone(alerts.llm_failing(self.conn))

    def test_the_alert_reaches_the_strip_and_names_the_doctor(self):
        self._run(status_transport(404))
        a = alerts.llm_failing(self.conn)
        self.assertEqual(a["kind"], "llm-backend")
        self.assertEqual(a["severity"], "warn")
        self.assertEqual(a["link"], "/doctor")
        self.assertIn("Smart categorization failed on its last run", a["message"])
        self.assertIn("'test-model' is not loaded", a["message"])
        self.assertIn("1 merchants", a["message"])
        # build() folds it in like every other source, ranked with the warns
        built = alerts.build({"llm_failing": a})
        self.assertEqual([x["kind"] for x in built], ["llm-backend"])

    def test_the_doctor_shows_the_failed_run_as_a_red_row(self):
        self._run(status_transport(404))

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"data": [{"id": "test-model"}]}

        class _Client:
            def __init__(self, *a, **k):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def get(self, url):
                return _Resp()

        with patch.dict(os.environ, ENV), patch("httpx.Client", _Client):
            rows = doctor._llm_rows(self.conn)
        by_name = {r["name"]: r for r in rows}
        # the live probe can be green (the model is back) while the row
        # that matters says the last run failed — both are true
        self.assertTrue(by_name["model"]["ok"])
        self.assertFalse(by_name["last run"]["ok"])
        self.assertIn("'test-model' is not loaded", by_name["last run"]["detail"])
        self.assertIn("stalled", by_name["last run"]["detail"])

    def test_one_bad_batch_among_good_ones_is_not_a_dead_backend(self):
        for i in range(2, 25):
            add_raw_txn(self.conn, f"t{i}", "2025-07-01", 5, f"Shop {i}", raw={})
        n = {"calls": 0}

        def handler(request):
            n["calls"] += 1
            if n["calls"] == 1:
                return httpx.Response(500)
            import json as _j
            body = _j.loads(request.content)
            listed = body["messages"][1]["content"]
            k = sum(1 for line in listed.splitlines() if line.strip()
                    and line.strip()[0].isdigit())
            return httpx.Response(200, json={"choices": [{"message": {
                "content": _j.dumps({str(i): "FOOD_AND_DRINK"
                                     for i in range(1, k + 1)})}}]})
        with patch.dict(os.environ, ENV):
            stats = llm_categorize.run(self.conn, batch_size=5,
                                       transport=httpx.MockTransport(handler))
        self.assertGreater(stats["merchants_classified"], 0)
        self.assertIn("backend_error", stats)
        self.assertEqual(llm_categorize.last_run(self.conn)["state"], "done")
        self.assertIsNone(alerts.llm_failing(self.conn))


if __name__ == "__main__":
    unittest.main()
