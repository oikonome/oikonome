"""The answer-report, folder-import run and tax-document commit doors read
their bodies through the typed reader, and keep every answer a client
relied on.

Each hands a list to an engine reader that checks or skips its items
itself — the report's turns and blocks, a folder's plan, a reviewed
document's rows — so the door types the list and its length and passes the
items on untouched. Pinned here:

* the matrix: every key each door reads, sent as an int, a list, an object,
  a 10 kB string and a NUL character, never answers 5xx;
* the report's leniencies: a turn that is not an object is skipped, a
  `turns` that is not a list leaves the single-answer body to speak, a
  turn whose `blocks` is not a list is left out, and only a literal `true`
  is `partial` — none of them is a refusal;
* the folder plan and the document rows keep their own refusals, word for
  word ("files must be a list", "too many files (max N)", and the same for
  rows).
"""

import unittest
import uuid
from unittest import mock

from fastapi.testclient import TestClient

from oikonome.engine import assistant

from .test_bill_doors_refuse_wrong_types import BAD_VALUES
from .util import _ensure_db

TEXT = {"kind": "text", "text": "Spending held steady."}


class ReportFolderAndTaxdocDoors(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web import security
        security._limiter._hits.clear()
        cls.security = security
        cls.client = TestClient(appmod.app, raise_server_exceptions=False)
        r = cls.client.post("/api/signup", data={
            "email": f"repfold-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code in (200, 303), r.text

    def _report(self, body):
        self.security._limiter._hits.clear()
        with mock.patch.object(assistant, "available", return_value=True):
            return self.client.post("/api/assistant/report", json=body)

    def _post(self, path, body):
        self.security._limiter._hits.clear()
        return self.client.post("/api" + path, json=body)

    # ---- the matrix ----------------------------------------------------

    def test_no_wrongly_typed_key_answers_5xx(self):
        bases = (
            ("/assistant/report", {"question": "Q", "blocks": [TEXT]},
             ("question", "blocks", "turns", "partial")),
            ("/assistant/report", {"turns": [{"question": "Q",
                                              "blocks": [TEXT]}]},
             ("turns",)),
            ("/import/bulk/run", {"token": "no-such-token", "files": []},
             ("token", "files", "background", "password", "totp_code",
              "recovery_code")),
            ("/import/taxdoc/commit", {"token": "no-such-token", "rows": []},
             ("token", "rows")))
        failures = []
        for path, base, keys in bases:
            for key in keys:
                for bad in BAD_VALUES:
                    body = dict(base, **{key: bad})
                    r = (self._report(body) if path == "/assistant/report"
                         else self._post(path, body))
                    if r.status_code >= 500:
                        failures.append(f"{path} {key}={str(bad)[:12]!r} "
                                        f"-> {r.status_code}")
                # a turn's own keys, wrongly typed
                if path == "/assistant/report" and key == "turns":
                    for inner in ("question", "blocks"):
                        for bad in BAD_VALUES:
                            turn = {"question": "Q", "blocks": [TEXT],
                                    inner: bad}
                            r = self._report({"turns": [turn]})
                            if r.status_code >= 500:
                                failures.append(
                                    f"turns[].{inner}={str(bad)[:12]!r} -> "
                                    f"{r.status_code}")
        self.assertEqual(failures, [])

    # ---- the report keeps what it never refused ----------------------

    def test_a_turn_that_is_not_an_object_is_skipped(self):
        r = self._report({"turns": [7, "x", None, [1],
                                    {"question": "Kept", "blocks": [TEXT]}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("Kept", r.text)
        self.assertIn("answer", r.headers["content-disposition"])

    def test_turns_that_are_not_a_list_leave_the_single_answer_body(self):
        for turns in ("x", 7, {"question": "No"}, True):
            with self.subTest(turns=turns):
                r = self._report({"turns": turns, "question": "Single",
                                  "blocks": [TEXT]})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIn("Single", r.text)

    def test_a_turn_whose_blocks_are_not_a_list_is_left_out(self):
        for blocks in ("x", 7, {"kind": "text", "text": "No"}):
            with self.subTest(blocks=blocks):
                r = self._report({"turns": [
                    {"question": "Dropped", "blocks": blocks},
                    {"question": "Kept", "blocks": [TEXT]}]})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertIn("Kept", r.text)
                self.assertNotIn("Dropped", r.text)
        # alone, nothing is left to report
        r = self._report({"question": "Q", "blocks": "x"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertEqual(r.json()["detail"], "nothing to report")

    def test_only_a_literal_true_marks_a_conversation_partial(self):
        turns = [{"question": "Q", "blocks": [TEXT]}]
        r = self._report({"turns": turns, "partial": True})
        self.assertIn("its start was not loaded", r.text)
        for partial in ("true", 1, [True], None):
            with self.subTest(partial=partial):
                r = self._report({"turns": turns, "partial": partial})
                self.assertEqual(r.status_code, 200, r.text)
                self.assertNotIn("its start was not loaded", r.text)

    def test_the_shipped_conversation_body_still_downloads(self):
        # the web and mobile clients send the turns AND the first turn as
        # the single-answer body an older server understands
        turns = [{"question": "First?", "blocks": [TEXT]},
                 {"question": "Second?", "blocks": [TEXT]}]
        r = self._report({"turns": turns, "partial": False,
                          "question": "First?", "blocks": [TEXT]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("conversation", r.headers["content-disposition"])
        self.assertIn("Second?", r.text)

    # ---- the folder plan and the reviewed rows ------------------------

    def test_a_folder_plan_keeps_its_own_refusals(self):
        from oikonome.web import bulk_import
        n = bulk_import.MAX_FILES
        for files, words in (({"a": 1}, "files must be a list"),
                             ("x", "files must be a list"),
                             ([{}] * (n + 1), f"too many files (max {n})")):
            with self.subTest(files=str(files)[:12]):
                r = self._post("/import/bulk/run",
                               {"token": "no-such-token", "files": files})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["detail"], words)

    def test_document_rows_keep_their_own_refusals(self):
        from oikonome.engine import taxdocs
        n = taxdocs.MAX_ROWS
        for rows, words in (({"a": 1}, "rows must be a list"),
                            (7, "rows must be a list"),
                            ([0] * (n + 1), f"too many rows (max {n})")):
            with self.subTest(rows=str(rows)[:12]):
                r = self._post("/import/taxdoc/commit",
                               {"token": "no-such-token", "rows": rows})
                self.assertEqual(r.status_code, 400, r.text)
                self.assertEqual(r.json()["detail"], words)

    def test_an_empty_plan_or_no_rows_reach_the_engine_as_before(self):
        # absent, null and "" are an empty list, as `or []` read them; the
        # engine then answers for its unknown token
        for body in ({"token": "no-such-token"},
                     {"token": "no-such-token", "files": None},
                     {"token": "no-such-token", "files": ""},
                     {"token": "no-such-token", "files": []}):
            r = self._post("/import/bulk/run", body)
            self.assertEqual(r.status_code, 400, r.text)
            self.assertNotIn("must be a list", r.json()["detail"])
        for body in ({"token": "no-such-token"},
                     {"token": "no-such-token", "rows": None},
                     {"token": "no-such-token", "rows": ""}):
            r = self._post("/import/taxdoc/commit", body)
            self.assertEqual(r.status_code, 400, r.text)
            self.assertNotIn("must be a list", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
