"""A model reply the parser cannot use must never surface what was in the
prompt.

The categorization run sends the household's merchant names, Amazon item
titles and charge amounts to a model backend, and a backend that is broken
or misconfigured (wrong chat template, over-quantized, a proxy that just
reflects) answers by echoing that prompt straight back. The run then has to
record WHY it failed, and that sentence is read in four places the reply
itself must never reach: the run record in job_progress, the Today/daily-
email alert, the Doctor page every member including a viewer can read, and
the diagnostic bundle the feedback form mails off-instance under an
explicit "no transaction contents" promise.

So the invariant is not "the model usually behaves" — it is that a failure
message names WHICH parse failed and HOW MUCH came back, and carries no
piece of the reply at all.
"""

import json
import os
import unittest
from unittest.mock import patch

import httpx

from oikonome.engine import alerts, llm_categorize
from oikonome.engine.compat import as_date, jsonb
from oikonome.web import doctor

from .test_llm_categorize import ENV, add_raw_txn
from .util import make_db

# Synthetic household strings, distinctive enough that finding one in a
# surfaced string can only mean the prompt came back out.
MERCHANT = "Zzyzx Bodega Fictional"
ITEM = "quokka-shaped night light"
PAYEE = "Fictional Fitness Club"
MARKERS = ("Zzyzx", "quokka", "Fictional Fitness")


def echo_transport(wrap: bool = False):
    """A backend that answers with the prompt it was given.

    wrap=True puts braces around it, so the reply reaches the JSON decoder
    (the "brace-balanced but malformed" branch) instead of failing the
    brace search."""
    def handler(request):
        body = json.loads(request.content)
        listing = body["messages"][-1]["content"]
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "{" + listing + "}" if wrap else listing}}]})
    return httpx.MockTransport(handler)


class ParseFailureMessageTests(unittest.TestCase):
    """Every parse entry point, called directly: the error it raises is
    what _note_failure stores, so it is the string that must be clean."""

    ECHO = (f"1. {MERCHANT} — 3 charge(s), typical $42\n"
            f"2. {ITEM}\n3. {PAYEE} — $15 every month")

    def _assert_clean(self, msg: str) -> None:
        for marker in MARKERS:
            self.assertNotIn(marker, msg)
        self.assertIn("bytes", msg)          # the shape, not the substance

    def test_a_category_reply_that_is_not_json(self):
        with self.assertRaises(llm_categorize.LLMError) as cm:
            llm_categorize._parse(self.ECHO, 3)
        self._assert_clean(str(cm.exception))

    def test_a_category_reply_that_is_malformed_json(self):
        with self.assertRaises(llm_categorize.LLMError) as cm:
            llm_categorize._parse("{" + self.ECHO + "}", 3)
        self._assert_clean(str(cm.exception))

    def test_a_free_text_reply_that_is_not_json(self):
        with self.assertRaises(llm_categorize.LLMError) as cm:
            llm_categorize._parse_freetext(self.ECHO, 3)
        self._assert_clean(str(cm.exception))

    def test_a_free_text_reply_that_is_malformed_json(self):
        with self.assertRaises(llm_categorize.LLMError) as cm:
            llm_categorize._parse_freetext("{" + self.ECHO + "}", 3)
        self._assert_clean(str(cm.exception))

    def test_an_amazon_reply_that_is_not_json(self):
        rows = [{"items_json": [{"title": ITEM}], "memo": "", "seller": ""}]
        with patch.dict(os.environ, ENV):
            with self.assertRaises(llm_categorize.LLMError) as cm:
                llm_categorize._classify_amazon_batch(
                    rows, ["Shopping"], transport=echo_transport())
        self._assert_clean(str(cm.exception))

    def test_an_amazon_reply_that_is_malformed_json(self):
        rows = [{"items_json": [{"title": ITEM}], "memo": "", "seller": ""}]
        with patch.dict(os.environ, ENV):
            with self.assertRaises(llm_categorize.LLMError) as cm:
                llm_categorize._classify_amazon_batch(
                    rows, ["Shopping"], transport=echo_transport(wrap=True))
        self._assert_clean(str(cm.exception))

    def test_an_unknown_failure_is_reported_by_class_not_by_message(self):
        """backend_failure is the one funnel onto the run record, and it
        is handed exceptions nobody here wrote (the summary and tag passes
        catch Exception). Text we did not author may quote whatever it
        choked on, so only its class travels."""
        msg = llm_categorize.backend_failure(
            RuntimeError(f"choked on '1. {MERCHANT}'"), {"model": "m"})
        for marker in MARKERS:
            self.assertNotIn(marker, msg)
        self.assertIn("RuntimeError", msg)


class FailedRunSurfaceTests(unittest.TestCase):
    """End to end: an echoing backend, then every place the failure is
    read back."""

    def setUp(self):
        self.conn = make_db()
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        add_raw_txn(self.conn, "t1", "2025-07-01", 42, MERCHANT, raw={})
        add_raw_txn(self.conn, "t2", "2025-07-02", 10, "AMAZON.COM", raw={})
        self.conn.execute(
            """INSERT INTO amazon_orders (dedup_key, account, date, amount,
                   category, category_source, is_refund, items_json)
               VALUES ('o1','a',%s,-10,'Shopping','rules',0,%s)""",
            (as_date("2025-07-02"), jsonb([{"title": ITEM, "price": 10}])))
        self.conn.execute(
            "INSERT INTO amazon_matches (transaction_id, dedup_key) "
            "VALUES ('t2','o1')")
        self.conn.execute(
            """INSERT INTO bill_proposals (id, kind, bill_type, payee,
                   amount, frequency, "interval", evidence, status,
                   created_at)
               VALUES ('prop:1','add','occurrence',%s,15.49,'MONTHLY',1,
                       %s,'pending',now())""", (PAYEE, jsonb({})))

    def tearDown(self):
        self.conn.close()

    def _run(self, wrap=False):
        with patch.dict(os.environ, ENV):
            return llm_categorize.run(self.conn,
                                      transport=echo_transport(wrap))

    def _assert_clean(self, where: str, text: str) -> None:
        for marker in MARKERS:
            self.assertNotIn(marker, text, f"prompt content surfaced in {where}")

    def _doctor_rows(self):
        # the live /v1/models probe would be real network; the row under
        # test is the LAST RUN row, which reads the database
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
            return doctor._llm_rows(self.conn), doctor.bundle(self.tid)

    def _check_every_surface(self, stats):
        self.assertTrue(stats.get("backend_error"),
                        "the run must still report that it failed")
        self._assert_clean("the run stats", stats["backend_error"])

        stored = self.conn.execute(
            "SELECT progress::text AS p FROM job_progress WHERE id='llm'"
        ).fetchone()["p"]
        self._assert_clean("job_progress", stored)

        last = llm_categorize.last_run(self.conn)
        self.assertEqual(last["state"], "error")
        self._assert_clean("last_run", str(last))

        alert = alerts.llm_failing(self.conn)
        self.assertIsNotNone(alert)
        self._assert_clean("the alert", alert["message"])

        rows, bundle = self._doctor_rows()
        self._assert_clean("the Doctor rows", json.dumps(rows))
        self._assert_clean("the feedback bundle",
                           json.dumps(bundle, default=str))

    def test_an_echoed_prompt_reaches_no_surface(self):
        self._check_every_surface(self._run())

    def test_an_echoed_prompt_wrapped_in_braces_reaches_no_surface(self):
        self._check_every_surface(self._run(wrap=True))


class LegacyRunRecordTests(unittest.TestCase):
    """Records written before failures were classified may quote the raw
    reply. They are read by the same four surfaces, and the run that would
    overwrite one never happens on an instance whose backend was switched
    off — so the old text is dropped on the way out, not shown."""

    def setUp(self):
        self.conn = make_db()
        self.conn.execute(
            """INSERT INTO job_progress (id, state, progress)
               VALUES ('llm','error',%s::jsonb)""",
            (jsonb({"error": f"no JSON in response: '1. {MERCHANT} — "
                             f"3 charge(s), typical $42'",
                    "model": "test-model", "url": "http://llm.test",
                    "produced": 0, "pending": 1}),))

    def tearDown(self):
        self.conn.close()

    def test_an_unclassified_error_is_not_handed_back(self):
        last = llm_categorize.last_run(self.conn)
        self.assertEqual(last["state"], "error")
        self.assertNotIn("Zzyzx", str(last))
        self.assertTrue(last["error"], "the failure itself is still reported")
        self.assertNotIn("Zzyzx", alerts.llm_failing(self.conn)["message"])


if __name__ == "__main__":
    unittest.main()
