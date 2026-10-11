"""Tax-document import: deterministic SSA/transcript parsing,
LLM-assisted W-2/1040, preview token flow, and per-column merge commits
(a transcript never blanks SSA columns)."""

import json
import os
import time
import unittest
from unittest import mock

from oikonome.engine import taxdocs

from .util import make_db, write_config


def _png(size=(8, 8), mode="RGB") -> bytes:
    """A real, tiny W-2 scan: the image path reads an image's size before
    it decodes, and refuses bytes it cannot measure."""
    import io

    from PIL import Image
    buf = io.BytesIO()
    Image.new(mode, size, "white" if mode == "RGB" else 1).save(buf, "PNG")
    return buf.getvalue()


W2_PNG = _png()

SSA_XML = b"""<?xml version="1.0"?>
<osss:OnlineSocialSecurityStatementData xmlns:osss="http://ssa.gov/oss">
  <osss:EarningsRecord>
    <osss:Earnings startYear="2000" endYear="2000">
      <osss:FicaEarnings>1000</osss:FicaEarnings>
      <osss:MedicareEarnings>1000</osss:MedicareEarnings>
    </osss:Earnings>
    <osss:Earnings startYear="2024" endYear="2024">
      <osss:FicaEarnings>110000</osss:FicaEarnings>
      <osss:MedicareEarnings>120000</osss:MedicareEarnings>
    </osss:Earnings>
    <osss:Earnings startYear="2026" endYear="2026">
      <osss:FicaEarnings>-1</osss:FicaEarnings>
      <osss:MedicareEarnings>-1</osss:MedicareEarnings>
    </osss:Earnings>
  </osss:EarningsRecord>
</osss:OnlineSocialSecurityStatementData>"""

TRANSCRIPT = """IRS Return Transcript
TAX PERIOD: Dec. 31, 2024
WAGES, SALARIES, TIPS, ETC.: $100,000.00
TOTAL INCOME: $105,000.00
ADJUSTED GROSS INCOME: $95,000.00
TAXABLE INCOME: $80,000.00
TOTAL TAX LIABILITY TP FIGURES: $15,000.00
"""


class DeterministicParseTests(unittest.TestCase):
    def test_ssa_xml(self):
        rows = taxdocs.parse_ssa_xml(SSA_XML)
        self.assertEqual(len(rows), 2)               # -1 year skipped
        self.assertEqual(rows[0], {"year": 2000, "ss_earnings": 1000.0,
                                   "medicare_earnings": 1000.0})
        self.assertEqual(rows[1]["ss_earnings"], 110000.0)
        self.assertEqual(rows[1]["medicare_earnings"], 120000.0)

    def test_an_earnings_record_with_absurdly_many_years_is_refused(self):
        """Every parsed row is previewed, staged as JSON and echoed in the
        response. A working life is under a hundred years; a file claiming
        more is not an earnings record, and it must be refused rather than
        materialized."""
        years = b"".join(
            b'<Earnings startYear="%d"><FicaEarnings>1</FicaEarnings>'
            b"</Earnings>" % y for y in range(taxdocs.MAX_ROWS + 2))
        with self.assertRaises(ValueError) as cm:
            taxdocs.parse_ssa_xml(b"<r>" + years + b"</r>")
        self.assertIn("earnings", str(cm.exception))

    def test_transcript_text(self):
        row = taxdocs.parse_transcript_text(TRANSCRIPT)[0]
        self.assertEqual(row["year"], 2024)
        self.assertEqual(row["wages"], 100000.0)
        self.assertEqual(row["total_income"], 105000.0)
        self.assertEqual(row["agi"], 95000.0)
        self.assertEqual(row["taxable_income"], 80000.0)
        self.assertEqual(row["tax_paid"], 15000.0)

    def test_a_value_wrapped_onto_the_next_line_still_parses(self):
        row = taxdocs.parse_transcript_text(
            "TAX PERIOD: Dec. 31, 2024\n"
            "ADJUSTED GROSS INCOME:\n    $95,000.00\n")[0]
        self.assertEqual(row["agi"], 95000.0)


class TranscriptParseTimeTests(unittest.TestCase):
    """Parsing runs as one regex call per field and holds the GIL for the
    whole server while it runs, so its cost must stay linear in the size
    of the upload whatever the upload contains."""

    def _assert_fast(self, text):
        t0 = time.perf_counter()
        taxdocs.parse_transcript_text(text)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_many_blank_lines_parse_in_linear_time(self):
        self._assert_fast("TAX PERIOD: Dec. 31, 2024\n"
                          "ADJUSTED GROSS INCOME: $1\n" + "\n" * 200_000)

    def test_a_label_followed_by_a_long_space_run_parses_in_linear_time(self):
        for label in ("TOTAL INCOME", "TOTAL TAX LIABILITY",
                      "WAGES, SALARIES, TIPS, ETC."):
            with self.subTest(label=label):
                self._assert_fast("TAX PERIOD: Dec. 31, 2024\n"
                                  + label + " " * 200_000 + "x\n")


class AnalyzeCommitTests(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _annual(self, year):
        return self.conn.execute(
            "SELECT * FROM income_annual WHERE year=%s", (year,)).fetchone()

    def test_ssa_then_transcript_merge(self):
        out = taxdocs.analyze(self.conn, "earnings.xml", SSA_XML, "text/xml")
        self.assertEqual(out["kind"], "ssa")
        self.assertFalse(out["rows"][0]["exists"])
        res = taxdocs.commit(self.conn, out["token"], out["rows"])
        self.assertEqual((res["created"], res["updated"]), (2, 0))
        # transcript for the same year merges — SSA columns survive
        out = taxdocs.analyze(self.conn, "transcript.txt",
                              TRANSCRIPT.encode(), "text/plain")
        self.assertEqual(out["kind"], "transcript")
        self.assertTrue(out["rows"][0]["exists"])
        res = taxdocs.commit(self.conn, out["token"], out["rows"])
        self.assertEqual((res["created"], res["updated"]), (0, 1))
        row = self._annual(2024)
        self.assertEqual(row["ss_earnings"], 110000.0)     # kept
        self.assertEqual(row["agi"], 95000.0)             # added
        self.assertEqual(row["source"], "ssa+transcript")

    def test_user_edits_rows_before_commit(self):
        out = taxdocs.analyze(self.conn, "earnings.xml", SSA_XML, "text/xml")
        rows = [r for r in out["rows"] if r["year"] == 2024]
        rows[0]["ss_earnings"] = 111111.0                  # edited in preview
        taxdocs.commit(self.conn, out["token"], rows)
        self.assertEqual(self._annual(2024)["ss_earnings"], 111111.0)
        self.assertIsNone(self._annual(2000))              # deselected row

    def test_commit_token_is_one_shot(self):
        out = taxdocs.analyze(self.conn, "earnings.xml", SSA_XML, "text/xml")
        taxdocs.commit(self.conn, out["token"], out["rows"])
        with self.assertRaises(ValueError):
            taxdocs.commit(self.conn, out["token"], out["rows"])

    def test_implausible_year_rejected(self):
        out = taxdocs.analyze(self.conn, "earnings.xml", SSA_XML, "text/xml")
        with self.assertRaises(ValueError):
            taxdocs.commit(self.conn, out["token"], [{"year": 12}])

    def test_w2_via_mocked_vision(self):
        payload = {"year": 2024, "employer": "ACME STAFFING",
                   "ein": "12-3456789",
                   "boxes": {"1": 50000.0, "2": 7000, "5": 52000}}
        with mock.patch("oikonome.engine.llm_categorize._chat",
                        return_value=json.dumps(payload)), \
             mock.patch("oikonome.engine.llm_categorize._backend",
                        return_value={"url": "http://localhost:11434",
                                      "model": "vl", "api_key": "",
                                      "extra_body": ""}):
            out = taxdocs.analyze(self.conn, "w2.png", W2_PNG,
                                  "image/png")
            self.assertEqual(out["kind"], "w2")
            res = taxdocs.commit(self.conn, out["token"], out["rows"])
        self.assertEqual(res["documents"], 1)
        doc = self.conn.execute(
            "SELECT * FROM income_documents WHERE year=2024").fetchone()
        self.assertEqual(doc["form"], "W-2")
        self.assertEqual(doc["payer"], "ACME STAFFING")
        self.assertEqual(doc["primary_amount"], 50000.0)
        # primary_wages filled because the year had none
        self.assertEqual(self._annual(2024)["primary_wages"], 50000.0)

    def test_1040_via_mocked_llm(self):
        payload = {"year": 2023, "total_income": 190000, "agi": 185000,
                   "taxable_income": 158000, "tax_paid": 28000,
                   "wages": 180000}
        text = b"Form 1040 U.S. Individual Income Tax Return 2023 ..."
        with mock.patch("oikonome.engine.llm_categorize._chat",
                        return_value=json.dumps(payload)), \
             mock.patch("oikonome.engine.llm_categorize._backend",
                        return_value={"url": "http://localhost:11434",
                                      "model": "m", "api_key": "",
                                      "extra_body": ""}):
            out = taxdocs.analyze(self.conn, "return-2023.txt", text,
                                  "text/plain")
            self.assertEqual(out["kind"], "1040")
            taxdocs.commit(self.conn, out["token"], out["rows"])
        self.assertEqual(self._annual(2023)["agi"], 185000.0)

    def test_tax_documents_will_not_leave_the_box_without_saying_so(self):
        """A W-2/1040 carries a full SSN, and the vision path ships
        the whole page to whatever OpenAI-compatible endpoint the tenant
        configured — which may legitimately be a third party."""
        remote = {"url": "https://api.example-llm.com/v1", "model": "m",
                  "api_key": "", "extra_body": ""}
        with mock.patch("oikonome.engine.llm_categorize._backend",
                        return_value=remote):
            with self.assertRaises(ValueError) as cm:
                taxdocs.parse_1040_text(self.conn, "Form 1040 ... AGI 1")
            msg = str(cm.exception)
            self.assertIn("Social Security number", msg)
            # The message must point at something the user can actually DO:
            # the Settings control, never the raw config key, and
            # test_api_manage pins that the control exists.
            self.assertIn("Settings", msg)
            self.assertNotIn("taxdocs_allow_remote_llm", msg,
                             "don't hand the user a raw config key")

            # explicit opt-in lets it through
            write_config(self.conn, taxdocs_allow_remote_llm="1")
            with mock.patch("oikonome.engine.llm_categorize._chat",
                            return_value=json.dumps({"year": 2023,
                                                     "agi": 1.0})):
                self.assertEqual(
                    taxdocs.parse_1040_text(self.conn, "x")[0]["year"], 2023)


    def test_the_consent_switch_is_actually_reachable(self):
        """A gate whose escape hatch has no write path is a dead end, not a
        fix. Pin the whole path: the key round-trips
        through POST /api/settings, and the SPA renders a control for it."""
        import pathlib as _p
        root = _p.Path(__file__).resolve().parents[2]
        api_src = (root / "server" / "oikonome" / "web" / "api.py").read_text()
        # a write path exists in the settings handler
        self.assertIn('if "taxdocs_allow_remote_llm" in body:', api_src)
        # ...and it is readable back, so the SPA can show its current state
        self.assertIn('"taxdocs_allow_remote_llm"', api_src)
        spa = (root / "webapp" / "src" / "pages" / "Settings.tsx").read_text()
        self.assertIn("taxdocs_allow_remote_llm: taxOk", spa)
        self.assertIn("Allow sensitive documents to be sent", spa)

    def test_local_llm_needs_no_extra_consent(self):
        """The self-host default is the bundled Ollama — nothing leaves the
        machine, so the gate must not nag there."""
        for url in ("http://localhost:11434", "http://ollama:11434",
                    "http://192.168.1.9:11434", "http://127.0.0.1:1234"):
            self.assertTrue(taxdocs._endpoint_is_local(url), url)
        # 203.0.113.x is RFC 5737 documentation space and is NOT globally
        # routable, so it reads as local — use a genuinely public address
        for url in ("https://api.openai.com/v1", "http://8.8.8.8:11434"):
            self.assertFalse(taxdocs._endpoint_is_local(url), url)

    def test_local_looking_name_that_resolves_public_is_not_local(self):
        """The off-box SSN-consent gate keys on _endpoint_is_local. A name
        judged by its spelling alone is a hole: ``llm.corp.internal`` or a
        dotless service name that actually resolves to the public internet
        (a rebinding or attacker-controlled record) must NOT be treated as
        local, or a full-SSN page image ships off-box with no consent."""
        from oikonome.web import netguard
        # .internal name that resolves to a real public address
        with mock.patch.object(netguard, "_resolved_ips",
                               return_value=["8.8.8.8"]):
            self.assertFalse(
                taxdocs._endpoint_is_local("http://llm.corp.internal:8080"))
        # mapped-IPv6 spelling of a public address, as an A/AAAA answer
        with mock.patch.object(netguard, "_resolved_ips",
                               return_value=["::ffff:8.8.8.8"]):
            self.assertFalse(
                taxdocs._endpoint_is_local("http://sneaky.example:11434"))
        # a name that resolves to a LAN address is genuinely local
        with mock.patch.object(netguard, "_resolved_ips",
                               return_value=["192.168.1.9"]):
            self.assertTrue(
                taxdocs._endpoint_is_local("http://myllm.lan:11434"))

    def test_consent_gate_still_fires_for_public_resolving_name(self):
        """End to end: point the vision backend at a local-LOOKING name that
        resolves public, and the SSN off-box consent gate must still raise."""
        from oikonome.web import netguard
        remote = {"url": "http://llm.corp.internal:8080/v1", "model": "m",
                  "api_key": "", "extra_body": ""}
        with mock.patch("oikonome.engine.llm_categorize._backend",
                        return_value=remote), \
             mock.patch.object(netguard, "_resolved_ips",
                               return_value=["8.8.8.8"]):
            with self.assertRaises(ValueError) as cm:
                taxdocs.parse_1040_text(self.conn, "Form 1040 ... AGI 1")
            self.assertIn("Social Security number", str(cm.exception))

    def test_a_plain_text_upload_is_capped_like_extracted_pdf_text(self):
        from oikonome.sync.pdfimport import MAX_TEXT_BYTES
        data = (b"TAX PERIOD: Dec. 31, 2024\nADJUSTED GROSS INCOME: $1\n"
                + b"x" * MAX_TEXT_BYTES)
        with self.assertRaises(ValueError) as cm:
            taxdocs.analyze(self.conn, "big.txt", data, "text/plain")
        self.assertIn("larger", str(cm.exception))

    def test_a_pdf_whose_forms_inflate_is_refused_with_bounded_memory(self):
        import tracemalloc

        from .test_pdfimport import many_inflating_forms_pdf
        pdf = many_inflating_forms_pdf(200)
        tracemalloc.start()
        try:
            with self.assertRaises(ValueError) as cm:
                taxdocs.analyze(self.conn, "return.pdf", pdf,
                                "application/pdf")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 12_000_000)
        self.assertIn("size limit", str(cm.exception))

    def test_unknown_document_says_what_is_supported(self):
        with self.assertRaises(ValueError) as cm:
            taxdocs.analyze(self.conn, "junk.txt", b"grocery list", "text/plain")
        self.assertIn("ssa.gov", str(cm.exception))


if __name__ == "__main__":
    unittest.main()


class CommitIsAllOrNothingTests(unittest.TestCase):
    """A bad row must not leave a half-imported document with no way back.

    A bad year on row five must leave rows one to four unwritten and the
    single-use preview token unspent, so the document can be corrected and
    retried.
    """

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _staged(self, kind="irs"):
        from oikonome.db import staging
        return staging.put(self.conn, "taxdoc", files=[("t.txt", b"x")],
                           meta={"kind": kind})

    def test_a_bad_row_writes_nothing_and_keeps_the_preview(self):
        token = self._staged()
        before = self.conn.execute(
            "SELECT count(*) AS n FROM income_annual").fetchone()["n"]
        with self.assertRaises(ValueError):
            taxdocs.commit(self.conn, token,
                           [{"year": 2020, "agi": 50000.0},
                            {"year": "not-a-year", "agi": 60000.0}])
        after = self.conn.execute(
            "SELECT count(*) AS n FROM income_annual").fetchone()["n"]
        self.assertEqual(before, after, "the good row must not have landed")
        # and the preview survived, so the same token still works
        out = taxdocs.commit(self.conn, token, [{"year": 2020, "agi": 50000.0}])
        self.assertEqual(out["created"] + out["updated"], 1)

    def test_a_clean_list_still_commits(self):
        token = self._staged()
        out = taxdocs.commit(self.conn, token,
                             [{"year": 2018, "agi": 10.0},
                              {"year": 2019, "agi": 20.0}])
        self.assertEqual(out["created"] + out["updated"], 2)


class EinIsNotKeptTests(unittest.TestCase):
    """A W-2's EIN is an identifier, so the full number is not kept.

    `business_entity.ein` is encrypted at rest, surfaced only as
    `ein_last4`, and excluded from the export. `income_documents.ein` is the
    same nine digits off a W-2, in a table that IS in the export list, and
    nothing reads it back — so it is not stored at all.
    """

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def test_only_the_last_four_are_stored(self):
        taxdocs._insert_income_document(
            self.conn, 2021, "ACME INC", "12-3456789", 50000.0, "{}")
        r = self.conn.execute(
            "SELECT ein, ein_last4 FROM income_documents "
            "WHERE year=2021").fetchone()
        self.assertIsNone(r["ein"], "the full EIN must not be stored")
        self.assertEqual(r["ein_last4"], "6789")

    def test_the_preview_and_the_staged_row_carry_only_the_last_four(self):
        """The full number has no reader anywhere, so it must not survive
        the trip either: the commit keeps four digits, and the analyze
        response and the staging row must not carry the rest."""
        conn = make_db()
        try:
            write_config(conn)
            payload = {"year": 2024, "employer": "ACME STAFFING",
                       "ein": "12-3456789", "boxes": {"1": 1000}}
            with mock.patch("oikonome.engine.llm_categorize._chat",
                            return_value=json.dumps(payload)), \
                 mock.patch("oikonome.engine.llm_categorize._backend",
                            return_value={"url": "http://localhost:11434",
                                          "model": "vl", "api_key": "",
                                          "extra_body": ""}):
                out = taxdocs.analyze(conn, "w2.png", W2_PNG,
                                      "image/png")
            self.assertNotIn("3456789", json.dumps(out))
            self.assertEqual(out["rows"][0]["ein"], "•••••6789")
            staged = conn.execute(
                "SELECT meta FROM import_staging "
                "WHERE kind='taxdoc' AND meta IS NOT NULL").fetchone()
            self.assertNotIn("3456789", json.dumps(staged["meta"]))
            # and the masked value still commits to the same four digits
            taxdocs.commit(conn, out["token"], out["rows"])
            r = conn.execute("SELECT ein_last4 FROM income_documents "
                             "WHERE year=2024").fetchone()
            self.assertEqual(r["ein_last4"], "6789")
        finally:
            conn.close()

    def test_a_malformed_ein_stores_nothing_rather_than_a_fragment(self):
        for bad in (None, "", "12", "n/a"):
            self.assertIsNone(taxdocs._ein_last4(bad), bad)

    def test_formatting_is_ignored(self):
        self.assertEqual(taxdocs._ein_last4("12-3456789"), "6789")
        self.assertEqual(taxdocs._ein_last4("123456789"), "6789")
        self.assertEqual(taxdocs._ein_last4(" 12 3456789 "), "6789")


class UntrustedModelOutputTests(unittest.TestCase):
    """The vision/chat model's JSON is untrusted input: a null year, a
    non-object `boxes`, or a bare-number reply must read as a bad parse
    (ValueError → the upload endpoint's 400), never crash analyze — the
    endpoint only catches ValueError, so a TypeError/AttributeError here
    reaches the browser as a 500."""

    LOCAL = {"url": "http://localhost:11434", "model": "m", "api_key": "",
             "extra_body": "", "vision_model": ""}

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _analyze(self, reply, filename="w2.png", data=W2_PNG,
                 mime="image/png"):
        with mock.patch("oikonome.engine.llm_categorize._backend",
                        return_value=dict(self.LOCAL)), \
             mock.patch("oikonome.engine.llm_categorize._chat",
                        return_value=json.dumps(reply)):
            return taxdocs.analyze(self.conn, filename, data, mime)

    def test_null_year_is_a_bad_read_not_a_crash(self):
        with self.assertRaises(ValueError):
            self._analyze({"year": None})                       # W-2 path
        with self.assertRaises(ValueError):
            self._analyze({"year": None}, filename="ret.txt",   # 1040 path
                          data=b"Form 1040 individual return",
                          mime="text/plain")

    def test_non_numeric_year_is_a_bad_read_not_a_crash(self):
        with self.assertRaises(ValueError):
            self._analyze({"year": "unknown"})

    def test_non_object_boxes_are_dropped_not_fatal(self):
        out = self._analyze({"year": 2024, "employer": "ACME",
                             "boxes": [1, 2, 3]})
        self.assertEqual(out["rows"][0]["boxes"], {})
        self.assertIsNone(out["rows"][0]["box1"])

    def test_the_whole_answer_wins_over_a_quoted_year_fragment(self):
        """A model that answers and then quotes a line of the form (or a
        prior year) as JSON: the object carrying the prompt's schema is the
        answer, not whichever object came last."""
        answer = {"year": 2023, "total_income": 190000, "agi": 185000,
                  "taxable_income": 158000, "tax_paid": 28000,
                  "wages": 180000}
        reply = (json.dumps(answer) + "\n\nNote: the return also shows the "
                 'prior year as {"year": 2022}.')
        with mock.patch("oikonome.engine.llm_categorize._backend",
                        return_value=dict(self.LOCAL)), \
             mock.patch("oikonome.engine.llm_categorize._chat",
                        return_value=reply):
            out = taxdocs.analyze(self.conn, "ret.txt",
                                  b"Form 1040 individual return",
                                  "text/plain")
            self.assertEqual(out["rows"][0]["year"], 2023)
            self.assertEqual(out["rows"][0]["agi"], 185000.0)
            w2 = {"year": 2024, "employer": "ACME", "ein": None,
                  "boxes": {"1": 50000}}
            with mock.patch("oikonome.engine.llm_categorize._chat",
                            return_value='{"year": 2019} then '
                            + json.dumps(w2) + ' and {"year": 2020}'):
                out = taxdocs.analyze(self.conn, "w2.png", W2_PNG,
                                      "image/png")
            self.assertEqual(out["rows"][0]["year"], 2024)
            self.assertEqual(out["rows"][0]["box1"], 50000)

    def test_non_object_reply_is_unparseable(self):
        for reply in (42, [1, 2], "hi", None):
            with self.assertRaises(ValueError, msg=repr(reply)):
                self._analyze(reply)


class VisionRoleConsentAgreementTests(unittest.TestCase):
    """The off-box consent gate and the outbound vision call resolve the
    backend independently — each calls _backend(role='vision'). If a
    future edit changed ONE call site's role, an SSN-bearing document
    could be sent to a backend the consent check never evaluated. With
    real settings routing categorize and vision at different backends,
    pin that both call sites resolve the VISION role and that the request
    targets the same URL the consent gate cleared."""

    LOCAL_URL = "http://127.0.0.1:11434"
    REMOTE_URL = "https://api.example-llm.com/v1"

    def setUp(self):
        self.conn = make_db()

    def tearDown(self):
        self.conn.close()

    def _route(self, categorize, vision):
        write_config(
            self.conn,
            llm_backends=[
                {"id": "loc", "name": "local box", "url": self.LOCAL_URL,
                 "model": "m-loc"},
                {"id": "cloud", "name": "remote", "url": self.REMOTE_URL,
                 "model": "m-cloud"}],
            llm_roles={"categorize": categorize, "vision": vision})

    def test_consent_judges_the_vision_backend_not_categorize(self):
        # categorize local, vision remote: the gate must still refuse —
        # the document travels on the VISION route
        self._route(categorize="loc", vision="cloud")
        with self.assertRaises(ValueError) as cm:
            taxdocs.parse_1040_text(self.conn, "Form 1040 ...")
        self.assertIn("Social Security number", str(cm.exception))

    def test_outbound_call_targets_what_consent_evaluated(self):
        # categorize remote, vision local: no consent needed, and the
        # request must go to the vision-routed (local) endpoint
        self._route(categorize="cloud", vision="loc")
        from oikonome.engine import llm_categorize
        real = llm_categorize._backend
        roles = []

        def spy(conn=None, role="categorize"):
            roles.append(role)
            return real(conn, role=role)

        with mock.patch.object(llm_categorize, "_backend",
                               side_effect=spy), \
             mock.patch.object(llm_categorize, "_chat",
                               return_value=json.dumps(
                                   {"year": 2023, "agi": 1.0})) as chat:
            rows = taxdocs.parse_1040_text(self.conn, "x")
        self.assertEqual(rows[0]["year"], 2023)
        self.assertEqual(roles, ["vision", "vision"],
                         "both the consent gate and the call itself must "
                         "resolve the vision role")
        self.assertEqual(chat.call_args.kwargs["backend"]["url"],
                         self.LOCAL_URL)


class CommitRefusesOutOfRangeInputTests(unittest.TestCase):
    """Client-edited rows that cannot be numbers or text are refused.

    The route turns ValueError into a 400. A 400-digit JSON integer, an
    Infinity year or an object where text belongs must be refused the same
    way, not escape as OverflowError or a database adapter error (a 500),
    and the preview must survive for a corrected retry.
    """

    @classmethod
    def setUpClass(cls):
        cls.conn = make_db()

    @classmethod
    def tearDownClass(cls):
        cls.conn.close()

    def _staged(self, kind):
        from oikonome.db import staging
        return staging.put(self.conn, "taxdoc", files=[("t.txt", b"x")],
                           meta={"kind": kind})

    def _refused(self, kind, rows):
        token = self._staged(kind)
        with self.assertRaises(ValueError):
            taxdocs.commit(self.conn, token, rows)
        # still usable: the refusal spent nothing
        taxdocs.commit(self.conn, token, [{"year": 2020}])

    def test_a_huge_integer_box_is_refused(self):
        self._refused("w2", [{"year": 2024, "boxes": {"1": 10 ** 400}}])

    def test_a_huge_integer_annual_amount_is_refused(self):
        self._refused("irs", [{"year": 2024, "wages": 10 ** 400}])

    def test_an_infinite_year_is_refused(self):
        self._refused("irs", [{"year": float("inf"), "agi": 1.0}])

    def test_an_object_employer_is_refused(self):
        self._refused("w2", [{"year": 2024, "employer": {"x": 1},
                              "boxes": {"1": 10.0}}])

    def test_an_object_ein_is_refused(self):
        self._refused("w2", [{"year": 2024, "ein": ["12"],
                              "boxes": {"1": 10.0}}])

    def test_rows_that_are_not_a_list_are_refused(self):
        token = self._staged("irs")
        with self.assertRaises(ValueError):
            taxdocs.commit(self.conn, token, 5)

    def test_ordinary_w2_still_commits(self):
        token = self._staged("w2")
        out = taxdocs.commit(self.conn, token,
                             [{"year": 2023, "employer": "Example Co",
                               "ein": "12-3456789", "boxes": {"1": 1000}}])
        self.assertEqual(out["documents"], 1)


class W2ImageDecodeIsBoundedTests(unittest.TestCase):
    """The W-2 image path decodes with the receipt decoder, so it keeps the
    receipt decoder's bounds: the byte cap, the pixel ceiling (an image
    whose size cannot be read is refused, never sent on raw), the shared
    decode gate, and no decode at all when no model is configured."""

    LOCAL = {"url": "http://localhost:11434", "model": "vl", "api_key": "",
             "extra_body": "", "vision_model": ""}
    REPLY = json.dumps({"year": 2024, "employer": "ACME", "boxes": {}})

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def _parse(self, data, mime="image/png", backend=None):
        with mock.patch("oikonome.engine.llm_categorize._backend",
                        return_value=dict(backend or self.LOCAL)), \
             mock.patch("oikonome.engine.llm_categorize._chat",
                        return_value=self.REPLY) as chat:
            try:
                return taxdocs.parse_w2(self.conn, data, mime), chat
            except ValueError:
                self.assertFalse(chat.called, "refused bytes reached the model")
                raise

    def test_a_declared_pixel_bomb_is_refused_before_decode(self):
        from oikonome.engine import receipts
        side = int(receipts.MAX_DECODE_PX ** 0.5) + 10
        bomb = _png((side, side), mode="1")    # a few KB, ~40 MP declared
        self.assertLess(len(bomb), receipts.MAX_IMAGE)
        with mock.patch("oikonome.engine.receipts._shrink_image") as shrink:
            with self.assertRaises(ValueError):
                self._parse(bomb)
            self.assertFalse(shrink.called)

    def test_unreadable_bytes_are_refused_not_passed_through(self):
        with self.assertRaises(ValueError):
            self._parse(b"\x89PNG not really an image")

    def test_an_image_past_the_receipt_byte_cap_is_refused(self):
        from oikonome.engine import receipts
        with self.assertRaises(ValueError):
            self._parse(W2_PNG + b"\0" * receipts.MAX_IMAGE)

    def test_a_busy_gate_refuses_instead_of_decoding(self):
        from oikonome.engine import receipts
        with mock.patch.object(receipts, "_DECODE_WAIT_S", 0.01):
            held = [receipts._DECODE_GATE.acquire(timeout=1)
                    for _ in range(int(os.environ.get(
                        "OIKONOME_RECEIPT_PARSE_CONCURRENCY", "2")))]
            try:
                with self.assertRaises(ValueError) as cm:
                    self._parse(W2_PNG)
                self.assertEqual(str(cm.exception), taxdocs.BUSY_ERROR)
            finally:
                for ok in held:
                    if ok:
                        receipts._DECODE_GATE.release()
        rows, _ = self._parse(W2_PNG)
        self.assertEqual(rows[0]["year"], 2024)

    def test_no_model_means_no_decode(self):
        with mock.patch("oikonome.engine.receipts._shrink_image") as shrink:
            with self.assertRaises(ValueError):
                self._parse(W2_PNG, backend={**self.LOCAL, "url": ""})
            self.assertFalse(shrink.called)
