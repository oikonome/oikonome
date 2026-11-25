"""The documented API shapes are true, complete for each migrated area, and
name exactly the keys each door reads.

The shapes in web/schemas reach both clients' types (specs/api-types.md),
so a wrong one misleads a client's type checker as surely as a missing
one. Pinned here:

* the conformance hook (tests/api_conformance.py) is installed for the whole
  suite, and refuses a reply with a missing, mistyped or undeclared field;
* in every area listed in MIGRATED, every door documents its reply, and every
  door that reads a JSON body documents the body — a new door in a migrated
  area cannot arrive undescribed;
* a documented body names exactly the keys its handler reads: a door that
  starts reading a key its model does not declare, or a model that declares
  a key no door reads, fails.
"""

from __future__ import annotations

import inspect
import os
import re
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from oikonome.web.fields import json_fields, json_fields_or_empty
from oikonome.web.schemas import Shape, body_of, documented, response_of

from . import api_conformance
from .test_api_types_are_generated_and_current import _generator
from .test_bill_doors_refuse_wrong_types import _harvest, _takes_raw_body

# The area modules whose doors are all documented. Grows one area at a
# time, smallest first (specs/api-types.md section 5).
MIGRATED: tuple[str, ...] = (
    "api_retirement", "api_continuity",
    "api_access", "api_reimburse", "api_assistant", "api_notify",
    "api_receipts", "api_bills",
    "api_today", "api_ledger",
    "api_imports", "api_accounts",
    "api_settings", "api_business",
    # the /api doors that live outside the area modules: sign-in, the
    # household's people and devices, the doctor (app.py), the reports,
    # the script read API and webhooks, help, logos and the export ticket
    "app", "reporting_api", "integrations", "help", "logos", "pages",
)

# doors in a migrated area whose 200 reply is not a JSON body (a file, a
# redirect), so there is no shape to document
NOT_JSON: frozenset[str] = frozenset({
    "assistant_report_api",        # the printable page, HTML
    "receipt_image",               # the image bytes
    "transactions_export",         # the CSV download
    "connections_export",          # the encrypted config file
    "business_export",             # the flagged rows as CSV
    "business_pnl_csv",            # the P&L as CSV
    "business_year_package",       # the year's tax package, a ZIP
    "compliance_ics",              # the calendar file
    "logo",                        # a merchant logo's image bytes
    "institution_logo",            # a bank's logo, PNG bytes
    "export_precheck",             # 204 when the download would start
})

# keys a handler reads through a helper the key harvest does not follow
_SIDE_READ = re.compile(
    r'_(?:scalar_text|literal_true|label_field|push_rows)\(\s*body\s*,\s*"([A-Za-z_]+)"')
# the shared harvest's patterns take letters and underscores only; a key
# with a digit in it (tin_last4) is read through the same accessors
_DIGIT_KEYS = re.compile(
    r'body\.\w+\(\s*"([A-Za-z_]*[0-9][A-Za-z0-9_]*)"')
# a body that may be absent is read as `(body or {}).get("key")`
_OPTIONAL_BODY = re.compile(r'\(body or \{\}\)\.get\(\s*"([A-Za-z_]+)"')

# helpers handed the whole body, and the keys each reads
_HELPER_KEYS = {
    "_step_up_proof(body)": ("password", "totp_code", "recovery_code"),
    "_consent_programs(body": ("consent",),
    "_report_blocks(body)": ("blocks",),
    "_alert_key(body)": ("kind", "message"),
    "_split_parts(body)": ("parts",),
    "_step_up_fields(body)": ("password", "totp_code", "recovery_code"),
    "_link_account_ids(body)": ("account_ids",),
    "_pin_kind(body)": ("kind",),
    "_enabled_flag(body)": ("enabled",),
    # the cookie half of a device mint: the step-up an older client sends,
    # and the device to sign out at the ceiling
    "_device_mint_session(request, user, body)": (
        "password", "totp_code", "recovery_code", "replace"),
    '_replace_device(conn, user["user_id"], body': ("replace",),
}


def _routes():
    os.environ["OIKONOME_DEV"] = "1"
    from oikonome.web.app import app
    return list(api_conformance._flat(app.routes))


def _reads_json_body(fn) -> bool:
    """Whether the door reads a JSON body: through the typed reader, or
    as a plain `dict = Body(...)`."""
    for p in inspect.signature(fn).parameters.values():
        dep = getattr(p.default, "dependency", None)
        if dep in (json_fields, json_fields_or_empty) or _takes_raw_body(p):
            return True
    return False


_EXCLUDED_MODULES = _generator().EXCLUDED_MODULES


def _client_door(route) -> bool:
    """A door the clients' types cover (the generator's own filter: /api,
    not a dev-only test door or a provider webhook). A module that also
    serves pages (app.py, pages.py) is migrated for its /api doors."""
    return (route.path.startswith("/api/")
            and route.endpoint.__module__ not in _EXCLUDED_MODULES)


# doors that take the whole body as a document and read it through their
# own rules (the settings save filters it by role, key by key); their body
# model declares what may be sent without itemising every key
_READ_WHOLE = frozenset({"settings_set"})

# keys a handler reads in a loop over a fixed tuple of names
_LOOP_READS = {
    "settings_outbound_resume": ("password", "totp_code", "recovery_code"),
    "import_mapped_api": ("date_col", "name_col", "amount_col", "debit_col",
                          "credit_col", "merchant_col", "category_col"),
}


def body_keys(fn) -> set[str]:
    """Every key the handler reads from its JSON body."""
    src = inspect.getsource(fn)
    keys = (set(_harvest(src)) | set(_SIDE_READ.findall(src))
            | set(_DIGIT_KEYS.findall(src))
            | set(_OPTIONAL_BODY.findall(src)))
    for call, read in _HELPER_KEYS.items():
        if call in src:
            keys.update(read)
    keys.update(_LOOP_READS.get(fn.__name__, ()))
    return keys


class TheHookRefusesAWrongReply(unittest.TestCase):
    """A small app of its own, so the hook is tested on replies known to be
    wrong rather than on the product's."""

    @classmethod
    def setUpClass(cls):
        class Pair(Shape):
            a: int
            b: str | None

        app = FastAPI()

        @app.get("/api/right")
        @documented(response=Pair)
        def right():
            return {"a": 1, "b": None}

        @app.get("/api/extra")
        @documented(response=Pair)
        def extra():
            return {"a": 1, "b": "x", "c": 2}

        @app.get("/api/mistyped")
        @documented(response=Pair)
        def mistyped():
            return {"a": "1", "b": "x"}

        @app.get("/api/missing")
        @documented(response=Pair)
        def missing():
            return {"a": 1}

        @app.get("/api/undocumented")
        def undocumented():
            return {"anything": True}

        cls.client = TestClient(app)

    def test_the_hook_is_installed(self):
        self.assertTrue(hasattr(TestClient.request, "__wrapped__"))

    def test_a_matching_reply_passes_and_is_counted(self):
        before = api_conformance.validated().get("GET /api/right", 0)
        self.assertEqual(self.client.get("/api/right").status_code, 200)
        self.assertEqual(api_conformance.validated()["GET /api/right"],
                         before + 1)

    def test_an_undeclared_a_mistyped_and_a_missing_field_fail(self):
        for path, word in (("/api/extra", "c"), ("/api/mistyped", "a"),
                           ("/api/missing", "b")):
            with self.subTest(path=path):
                with self.assertRaises(AssertionError) as ctx:
                    self.client.get(path)
                self.assertIn("Pair", str(ctx.exception))
                self.assertIn(word, str(ctx.exception))

    def test_an_undocumented_door_is_not_checked(self):
        self.assertEqual(self.client.get("/api/undocumented").status_code,
                         200)


class EachProcessReportsItsOwnCount(unittest.TestCase):
    """A sharded run is several processes exiting at different times; each
    writes its own report and the merge sums them, so the total is real."""

    def test_reports_are_per_process_and_sum(self):
        import json
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "conformance.json")
            with mock.patch.dict(os.environ,
                                 {"OIKONOME_API_CONFORMANCE_REPORT": path}), \
                    mock.patch.object(api_conformance, "_validated",
                                      {"GET /api/a": 2}):
                api_conformance._report()
            self.assertTrue(os.path.exists(f"{path}.{os.getpid()}"))
            self.assertFalse(os.path.exists(path))
            with open(f"{path}.1", "w") as f:
                json.dump({"validated": 4,
                           "doors": {"GET /api/a": 1, "PUT /api/b": 3}}, f)
            merged = api_conformance.merge_reports(path)
        self.assertEqual(merged["validated"], 6)
        self.assertEqual(merged["processes"], 2)
        self.assertEqual(merged["doors"], {"GET /api/a": 3, "PUT /api/b": 3})


class MigratedAreasAreComplete(unittest.TestCase):
    def test_every_door_in_a_migrated_area_documents_its_shapes(self):
        missing = []
        for r in _routes():
            mod = r.endpoint.__module__.rsplit(".", 1)[-1]
            if mod not in MIGRATED or not _client_door(r):
                continue
            where = f"{sorted(r.methods)} {r.path}"
            if r.endpoint.__name__ not in NOT_JSON \
                    and response_of(r.endpoint) is None:
                missing.append(f"{where}: no documented reply")
            if _reads_json_body(r.endpoint) and body_of(r.endpoint) is None:
                missing.append(f"{where}: no documented body")
        self.assertEqual(missing, [])

    def test_the_migrated_list_names_real_area_modules(self):
        mods = {r.endpoint.__module__.rsplit(".", 1)[-1] for r in _routes()}
        self.assertEqual(set(MIGRATED) - mods, set())


class DocumentedBodiesNameTheKeysTheDoorReads(unittest.TestCase):
    def test_model_fields_equal_the_keys_read(self):
        wrong = []
        for r in _routes():
            model = body_of(r.endpoint)
            if model is None or r.endpoint.__name__ in _READ_WHOLE:
                continue
            read = body_keys(r.endpoint)
            declared = {f.alias or n for n, f in model.model_fields.items()}
            if read != declared:
                wrong.append(
                    f"{sorted(r.methods)} {r.path} ({model.__name__}): "
                    f"read but undeclared {sorted(read - declared)}, "
                    f"declared but unread {sorted(declared - read)}")
        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main()
