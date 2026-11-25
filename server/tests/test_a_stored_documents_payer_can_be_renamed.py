"""A stored tax document's payer can be renamed after it was stored.

An IRS transcript names a payer by its name control, a few capital
letters, and a form read from a photo can carry a misread name; neither
could be corrected once stored. The rename door renames the payer on the
document's own forms — those under the payer the list showed — under the
years' locks the removal takes, against the list the person saw (a
stale list is refused), with no step-up: it loses nothing, no figure
reads a payer. The new name is length-capped like every other name and
the rename is in the activity log, as a removal is. All names invented.
"""

import os
import pathlib
import unittest
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy
from oikonome.engine import taxdocs

from .util import _ensure_db, make_db, write_config


def _two_year_upload(conn):
    conn.execute(
        """INSERT INTO income_documents (id, year, form, owner, payer,
                ein_last4, primary_amount) VALUES
             (21, 2022, '1099-INT', 'primary', 'QRTZ', '4321', 14),
             (22, 2023, '1099-INT', 'primary', 'QRTZ', '4321', 16)""")
    taxdocs.backfill_household(conn)
    # one upload holding both years' forms, as a transcript holds them
    ids = [r["upload_id"] for r in conn.execute(
        "SELECT upload_id FROM income_documents WHERE id IN (21, 22)")]
    conn.execute("UPDATE income_documents SET upload_id = %s "
                 "WHERE id IN (21, 22)", (ids[0],))
    conn.execute("DELETE FROM income_upload_values WHERE upload_id <> %s "
                 "AND upload_id = ANY(%s)", (ids[0], ids))
    return next(d for d in taxdocs.list_stored(conn)
                if d["id"] == ids[0])


class TheRenameDoor(unittest.TestCase):
    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)

    def tearDown(self):
        self.conn.close()

    def test_the_payer_is_renamed_on_the_documents_forms(self):
        doc = _two_year_upload(self.conn)
        self.assertEqual(doc["payer"], "QRTZ")
        years = taxdocs.rename_payer(self.conn, doc["id"], doc["seen"],
                                     "QRTZ", "Quartzline Savings")
        self.assertEqual(years, doc["years"])
        payers = {r["payer"] for r in self.conn.execute(
            "SELECT payer FROM income_documents WHERE upload_id = %s",
            (doc["id"],))}
        self.assertEqual(payers, {"Quartzline Savings"})
        shown = next(d for d in taxdocs.list_stored(self.conn)
                     if d["id"] == doc["id"])
        self.assertEqual((shown["employer_shown"], shown["abbreviated"]),
                         ("Quartzline Savings", False))

    def test_a_stale_list_or_payer_is_refused(self):
        doc = _two_year_upload(self.conn)
        stale = {**doc["seen"], "years": [2022]}
        with self.assertRaises(taxdocs.UploadChanged):
            taxdocs.rename_payer(self.conn, doc["id"], stale, "QRTZ", "X Co")
        with self.assertRaises(taxdocs.UploadChanged):
            taxdocs.rename_payer(self.conn, doc["id"], doc["seen"],
                                 "NOT IT", "X Co")

    def test_a_blank_name_is_refused(self):
        doc = _two_year_upload(self.conn)
        with self.assertRaises(ValueError):
            taxdocs.rename_payer(self.conn, doc["id"], doc["seen"],
                                 "QRTZ", "   ")


def _shown(conn, upload_id):
    d = next(d for d in taxdocs.list_stored(conn) if d["id"] == upload_id)
    return d["employer_shown"], d["abbreviated"]


class ATypedNameIsShownAsTyped(unittest.TestCase):
    """A name a person typed is never read as IRS name-control syntax: a
    parenthesis is part of it, capitals are not an abbreviation, and no
    other stored form with the same EIN speaks over it."""

    def setUp(self):
        self.conn = make_db()
        write_config(self.conn)
        self.doc = _two_year_upload(self.conn)

    def tearDown(self):
        self.conn.close()

    def _rename(self, name, conn=None):
        conn = conn or self.conn
        doc = next(d for d in taxdocs.list_stored(conn)
                   if d["id"] == self.doc["id"])
        taxdocs.rename_payer(conn, doc["id"], doc["seen"], doc["payer"],
                             name)

    def test_a_name_with_a_parenthesis_is_shown_whole(self):
        self._rename("Northwind Clinic (East Campus)")
        self.assertEqual(_shown(self.conn, self.doc["id"]),
                         ("Northwind Clinic (East Campus)", False))

    def test_an_all_caps_name_is_not_called_an_abbreviation(self):
        self._rename("QLSB")
        self.assertEqual(_shown(self.conn, self.doc["id"]), ("QLSB", False))

    def test_a_longer_name_under_the_same_ein_does_not_speak_over_it(self):
        self.conn.execute(
            """INSERT INTO income_documents (id, year, form, owner, payer,
                    ein_last4, primary_amount)
               VALUES (31, 2021, '1099-INT', 'primary',
                       'Quartzline Savings Bank', '4321', 9)""")
        self._rename("QLSB")
        self.assertEqual(_shown(self.conn, self.doc["id"]), ("QLSB", False))

    def test_a_stub_under_the_same_ein_is_shown_by_the_typed_name(self):
        self._rename("Quartzline")
        self.conn.execute(
            """INSERT INTO income_documents (id, year, form, owner, payer,
                    ein_last4, primary_amount)
               VALUES (32, 2021, '1099-INT', 'primary', 'QRTZ SAVI',
                       '4321', 9),
                      (5, 2021, '1099-INT', 'primary',
                       'Quartz (Main Office)', '4321', 9)""")
        self.assertEqual(taxdocs.payer_shown(self.conn, "QRTZ SAVI", "4321"),
                         ("Quartzline", False))

    def test_the_mark_survives_a_removal_and_its_put_back(self):
        self._rename("QLSB")
        doc = next(d for d in taxdocs.list_stored(self.conn)
                   if d["id"] == self.doc["id"])
        taxdocs.remove_upload(self.conn, doc["id"], doc["seen"])
        self.assertEqual(_shown(self.conn, self.doc["id"]), ("QLSB", False))
        doc = next(d for d in taxdocs.list_stored(self.conn)
                   if d["id"] == self.doc["id"])
        taxdocs.restore_upload(self.conn, doc["id"], doc["seen"])
        self.assertEqual(_shown(self.conn, self.doc["id"]), ("QLSB", False))

    def test_an_export_and_restore_keep_the_mark(self):
        from oikonome.sync import export, restore
        self._rename("QLSB")
        data = export.build_zip(self.conn)
        dest = make_db()
        try:
            restore.restore_outcomes(dest, data)
            shown = {r["payer"]: r["payer_named_by_person"]
                     for r in dest.execute(
                         "SELECT payer, payer_named_by_person "
                         "FROM income_documents")}
            self.assertEqual(shown, {"QLSB": True})
            up = next(d for d in taxdocs.list_stored(dest)
                      if d["payer"] == "QLSB")
            self.assertEqual((up["employer_shown"], up["abbreviated"]),
                             ("QLSB", False))
        finally:
            dest.close()


class TheRenameRoute(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.client = TestClient(appmod.app)
        r = cls.client.post("/api/signup", data={
            "email": f"pr-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        cls.tid = cls.client.get("/api/me").json()["tenant_id"]

    def test_a_two_year_document_is_renamed_without_a_step_up_and_logged(self):
        conn = tenancy.tenant_connect(self.tid)
        try:
            write_config(conn)
            doc = _two_year_upload(conn)
        finally:
            conn.close()
        # removing this document would ask for the step-up; renaming does not
        self.assertTrue(taxdocs._step_up_for(doc["kind"], doc["years"]))
        long = self.client.post(
            f"/api/import/taxdoc/stored/{doc['id']}/payer",
            json={"seen": doc["seen"], "payer": "QRTZ", "name": "Q" * 121})
        self.assertEqual(long.status_code, 400, long.text)
        r = self.client.post(
            f"/api/import/taxdoc/stored/{doc['id']}/payer",
            json={"seen": doc["seen"], "payer": "QRTZ",
                  "name": "Quartzline Savings"})
        self.assertEqual(r.status_code, 200, r.text)
        listed = self.client.get("/api/import/taxdoc/stored").json()
        row = next(d for d in listed["documents"] if d["id"] == doc["id"])
        self.assertEqual(row["employer_shown"], "Quartzline Savings")
        again = self.client.post(
            f"/api/import/taxdoc/stored/{doc['id']}/payer",
            json={"seen": doc["seen"], "payer": "QRTZ", "name": "Other"})
        self.assertEqual(again.status_code, 409, again.text)
        acts = self.client.get("/api/activity",
                               params={"kind": "settings"}).json()["rows"]
        self.assertIn("tax_document_payer_renamed",
                      [a.get("action") for a in acts])


class WhoMayRename(unittest.TestCase):
    """A rename changes how every form under the same EIN is shown, other
    people's included — household data that every member may edit, as they
    may remove the document outright; a viewer writes nothing."""

    PATH = "/api/import/taxdoc/stored/7/payer"

    def test_a_member_may_and_a_viewer_may_not(self):
        from oikonome.web import permissions
        self.assertFalse(permissions.owner_only(self.PATH))
        self.assertTrue(permissions.can_write("member", self.PATH))
        self.assertFalse(permissions.can_write("viewer", self.PATH))

    def test_the_rename_shows_on_another_documents_stub_under_the_ein(self):
        conn = make_db()
        try:
            write_config(conn)
            doc = _two_year_upload(conn)
            conn.execute(
                """INSERT INTO income_documents (id, year, form, owner,
                        payer, ein_last4, primary_amount)
                   VALUES (41, 2021, '1099-INT', 'spouse', 'QRTZ', '4321',
                           7)""")
            taxdocs.backfill_household(conn)
            other = next(d for d in taxdocs.list_stored(conn)
                         if d["id"] != doc["id"])
            self.assertEqual(_shown(conn, other["id"]), ("QRTZ", True))
            taxdocs.rename_payer(conn, doc["id"], doc["seen"], "QRTZ",
                                 "Quartzline Savings")
            self.assertEqual(_shown(conn, other["id"]),
                             ("Quartzline Savings", False))
            # the other document's own stored name is untouched
            self.assertEqual(conn.execute(
                "SELECT payer FROM income_documents WHERE id = 41"
            ).fetchone()["payer"], "QRTZ")
        finally:
            conn.close()


class BothClientsOfferIt(unittest.TestCase):
    ROOT = pathlib.Path(__file__).resolve().parents[2]

    def test_web_and_phone_rename_under_the_listed_payer(self):
        for page, api in (("webapp/src/pages/Import.tsx",
                           "webapp/src/api/client.ts"),
                          ("mobile/src/app/imports.tsx",
                           "mobile/src/lib/api.ts")):
            self.assertIn("rename payer", (self.ROOT / page).read_text(), page)
            self.assertIn("{ seen: d.seen, payer: d.payer, name }",
                          (self.ROOT / api).read_text(), api)


if __name__ == "__main__":
    unittest.main()
