"""Removing a business entity must never silently destroy its records.

The equity ledger, mileage log, 1099 vendors, compliance calendar, and
membership all hang off business_entity with ON DELETE CASCADE — tax-relevant
records with retention obligations. So the everyday "remove" is an ARCHIVE
(entity hidden from active use, every record survives, restorable), and the
cascades only ever fire on an explicit delete-forever that requires the
entity's exact typed name, enforced server-side.
"""
import datetime as dt
import unittest
import uuid

from oikonome.db import tenancy
from oikonome.engine import compliance, entities, equity, selfemploy

from .util import (_admin_dsn, _ensure_db, TEST_DB, add_txn, seed_accounts,
                   TODAY)

# every table whose rows cascade away with the entity, with the count of
# rows the fixture creates in it
_SUBTABLES = ("entity_membership", "equity_movement",
              "compliance_obligation", "mileage_log", "vendor_1099")


class _ArchiveBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.tid = str(tenancy.create_tenant(
            self.admin, f"arc-{uuid.uuid4().hex[:8]}"))
        self.conn = tenancy.tenant_connect(self.tid)
        seed_accounts(self.conn)
        self.ent = entities.create_entity(
            self.conn, name="Acme LLC", structure="single_member_llc",
            state="ID", formation_date="2025-03-12")
        eid = self.ent["id"]
        # one row in every cascade table, plus both assignment kinds
        entities.add_member(self.conn, eid, member_name="Jordan Avery",
                            ownership_pct=100)
        equity.record_movement(self.conn, eid, kind="contribution",
                               amount=500, date=TODAY.isoformat())
        compliance.add_obligation(self.conn, eid, title="Franchise tax",
                                  due_date=(TODAY + dt.timedelta(days=200)
                                            ).isoformat())
        selfemploy.add_trip(self.conn, eid, date=TODAY.isoformat(), miles=12.5)
        selfemploy.mark_vendor(self.conn, eid, "ACE CONTRACTING")
        entities.assign_account(self.conn, "chk", eid)
        self.txn = add_txn(self.conn, TODAY, 40.0, "OFFICE DEPOT",
                           account="card")
        entities.assign_transaction(self.conn, self.txn, eid)

    def tearDown(self):
        self.conn.close()
        self.admin.close()

    def _counts(self):
        return {t: self.conn.execute(
            f"SELECT count(*) AS n FROM {t} WHERE entity_id = %s",
            (self.ent["id"],)).fetchone()["n"] for t in _SUBTABLES}


class ArchivePreservesRecordsTests(_ArchiveBase):

    def test_archive_hides_from_active_use_but_keeps_every_row(self):
        before = self._counts()
        self.assertTrue(all(before.values()), before)
        ent = entities.archive_entity(self.conn, self.ent["id"])
        self.assertEqual(ent["status"], "archived")
        self.assertIsNotNone(ent["archived_at"])
        # hidden from active use: the picker's active-only list drops it...
        self.assertEqual(
            [], entities.list_entities(self.conn, include_archived=False))
        # ...and writes refuse it
        with self.assertRaises(ValueError):
            entities.require_active(self.conn, self.ent["id"])
        # ...but every sub-table row and both assignments survive, readable
        self.assertEqual(before, self._counts())
        self.assertEqual(
            "chk", self.conn.execute(
                "SELECT id FROM accounts WHERE entity_id = %s",
                (self.ent["id"],)).fetchone()["id"])
        self.assertEqual(
            self.txn, self.conn.execute(
                "SELECT id FROM transactions WHERE entity_id = %s "
                "AND account_id = 'card'", (self.ent["id"],)).fetchone()["id"])
        self.assertIsNotNone(entities.get_entity(self.conn, self.ent["id"]))

    def test_restore_round_trip_reopens_writes(self):
        entities.archive_entity(self.conn, self.ent["id"])
        ent = entities.restore_entity(self.conn, self.ent["id"])
        self.assertEqual(ent["status"], "active")
        self.assertIsNone(ent["archived_at"])
        # active again: back in the picker, writable again
        self.assertEqual(
            1, len(entities.list_entities(self.conn,
                                          include_archived=False)))
        entities.require_active(self.conn, self.ent["id"])   # no raise
        upd = entities.update_entity(self.conn, self.ent["id"],
                                     name="Acme Restored LLC")
        self.assertEqual(upd["name"], "Acme Restored LLC")

    def test_archived_entity_generates_no_future_derived_obligations(self):
        # a closed LLC with an estimated-tax rate owes no FUTURE
        # derived filings — but its stored custom obligations stay listed
        entities.update_entity(self.conn, self.ent["id"], income_tax_rate=20)
        active = compliance.obligations(
            self.conn, entities.get_entity(self.conn, self.ent["id"]),
            today=TODAY)
        self.assertEqual({"state", "federal", "custom"},
                         {o["source"] for o in active})
        entities.archive_entity(self.conn, self.ent["id"])
        archived = compliance.obligations(
            self.conn, entities.get_entity(self.conn, self.ent["id"]),
            today=TODAY)
        self.assertEqual(["custom"], [o["source"] for o in archived])

    def test_archived_entity_exports_no_future_derived_events(self):
        """The .ics carries its own copy of the archived-status split, so
        it needs its own guard: a closed business must not keep pushing
        annual-report and estimated-tax deadlines into the owner's
        calendar, while the obligations they wrote themselves stay."""
        entities.update_entity(self.conn, self.ent["id"], income_tax_rate=20)
        active = compliance.ics(
            self.conn, entities.get_entity(self.conn, self.ent["id"]),
            today=TODAY)
        self.assertIn("Idaho LLC annual report", active)
        self.assertIn("Federal estimated tax", active)
        self.assertIn("Franchise tax", active)

        entities.archive_entity(self.conn, self.ent["id"])
        archived = compliance.ics(
            self.conn, entities.get_entity(self.conn, self.ent["id"]),
            today=TODAY)
        self.assertNotIn("Idaho LLC annual report", archived)
        self.assertNotIn("Federal estimated tax", archived)
        # the user's own record survives, and the file is still a calendar
        self.assertIn("Franchise tax", archived)
        self.assertEqual(1, archived.count("BEGIN:VEVENT"))

    def test_archiving_again_after_a_restore_restamps_the_closing_date(self):
        """archived_at is the date the business closed, so a reopened
        entity that closes a second time must carry the SECOND closing,
        not the first and not null — retention and the compliance calendar
        both read it."""
        first = entities.archive_entity(self.conn, self.ent["id"])["archived_at"]
        self.assertIsNotNone(first)
        self.assertIsNone(
            entities.restore_entity(self.conn, self.ent["id"])["archived_at"])
        second = entities.archive_entity(self.conn, self.ent["id"])["archived_at"]
        self.assertIsNotNone(second, "second archive left archived_at null")
        self.assertGreater(second, first,
                           "second archive kept the first closing date")


class DeleteForeverTests(_ArchiveBase):

    def test_wrong_name_is_refused_and_destroys_nothing(self):
        before = self._counts()
        for wrong in ("", None, "acme llc", "Acme", "Acme LLC "
                      "Enterprises"):
            with self.assertRaises(ValueError):
                entities.delete_forever(self.conn, self.ent["id"], wrong)
        self.assertEqual(before, self._counts())
        self.assertIsNotNone(entities.get_entity(self.conn, self.ent["id"]))

    def test_exact_name_destroys_entity_and_cascades_detaches_money(self):
        impact = entities.delete_forever(self.conn, self.ent["id"],
                                         "Acme LLC")
        self.assertEqual(impact["destroyed"],
                         {"members": 1, "equity_movements": 1,
                          "compliance_obligations": 1, "mileage_trips": 1,
                          "vendors_1099": 1})
        self.assertEqual(impact["detached"],
                         {"accounts": 1, "transactions": 1})
        self.assertIsNone(entities.get_entity(self.conn, self.ent["id"]))
        self.assertEqual({t: 0 for t in _SUBTABLES}, self._counts())
        # money is DETACHED, never deleted: rows survive with the
        # assignment cleared (the SET NULL FKs)
        self.assertIsNone(self.conn.execute(
            "SELECT entity_id FROM accounts WHERE id = 'chk'"
        ).fetchone()["entity_id"])
        self.assertIsNone(self.conn.execute(
            "SELECT entity_id FROM transactions WHERE id = %s",
            (self.txn,)).fetchone()["entity_id"])

    def test_delete_impact_counts_without_touching_anything(self):
        before = self._counts()
        impact = entities.delete_impact(self.conn, self.ent["id"])
        self.assertEqual(impact["name"], "Acme LLC")
        self.assertEqual(sum(impact["destroyed"].values()), 5)
        self.assertEqual(before, self._counts())


class EntityEndpointCompatTests(unittest.TestCase):
    """The HTTP surface: a stale client's plain DELETE must archive (the
    safe default), delete-forever must demand the exact name server-side,
    and has_business must stay true for an archived-only tenant so the way
    back to the records (and the Restore button) stays reachable."""

    @classmethod
    def setUpClass(cls):
        import os
        from fastapi.testclient import TestClient
        _ensure_db()
        os.environ["OIKONOME_DEV"] = "1"
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from oikonome.web.app import app
        cls.client = TestClient(app)
        cls.client.post("/api/signup", data={
            "email": f"arc-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})

    def _create(self, name="Birchwood Supply LLC"):
        r = self.client.post("/api/business/entities", json={
            "name": name, "structure": "sole_prop"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_stale_delete_call_archives_instead_of_destroying(self):
        eid = self._create("Stale Client LLC")
        r = self.client.delete(f"/api/business/entities/{eid}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "archived")
        # still listed — nothing was destroyed
        listed = self.client.get("/api/business/entities").json()["entities"]
        self.assertIn(eid, [e["id"] for e in listed])
        # and restorable over the wire
        r = self.client.post(f"/api/business/entities/{eid}/restore")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "active")
        self.assertIsNone(r.json()["archived_at"])

    def test_delete_forever_requires_exact_name_in_body(self):
        eid = self._create("Typo Guard LLC")
        # a non-string confirmation is a refusal, not a crash — the raw
        # body value reaches the name comparison, so any JSON type must
        # land in the same 400
        for body in ({}, {"name": ""}, {"name": "typo guard llc"},
                     {"name": "Typo Guard"}, {"name": 12345},
                     {"name": True}, {"name": ["Typo Guard LLC"]},
                     {"name": {"name": "Typo Guard LLC"}}):
            r = self.client.post(
                f"/api/business/entities/{eid}/delete-forever", json=body)
            self.assertEqual(r.status_code, 400, r.text)
        listed = self.client.get("/api/business/entities").json()["entities"]
        self.assertIn(eid, [e["id"] for e in listed])
        # the exact name destroys it, and reports what went with it
        r = self.client.post(
            f"/api/business/entities/{eid}/delete-forever",
            json={"name": "Typo Guard LLC"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("destroyed", r.json())
        listed = self.client.get("/api/business/entities").json()["entities"]
        self.assertNotIn(eid, [e["id"] for e in listed])

    def test_archive_endpoint_and_impact_preview(self):
        eid = self._create("Impact Preview LLC")
        r = self.client.post(f"/api/business/entities/{eid}/archive")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "archived")
        r = self.client.get(f"/api/business/entities/{eid}/delete-impact")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["name"], "Impact Preview LLC")
        self.assertEqual(set(r.json()["destroyed"]),
                         {"members", "equity_movements",
                          "compliance_obligations", "mileage_trips",
                          "vendors_1099"})
        # clean up so the archived-only test below controls its own state
        self.client.post(f"/api/business/entities/{eid}/delete-forever",
                         json={"name": "Impact Preview LLC"})

    def test_has_business_stays_true_with_only_archived_entities(self):
        eid = self._create("Closed Doors LLC")
        # archive every entity this tenant has, so only archived remain
        for e in self.client.get(
                "/api/business/entities").json()["entities"]:
            self.client.post(f"/api/business/entities/{e['id']}/archive")
        me = self.client.get("/api/me").json()
        self.assertTrue(me["has_business"])
        # the Business page must still reach the archived entity to restore
        r = self.client.post(f"/api/business/entities/{eid}/restore")
        self.assertEqual(r.status_code, 200, r.text)


class DeleteForeverIsOwnerOnlyTests(unittest.TestCase):
    """Permanent destruction of a business entity — and the tax records that
    cascade off it — is reserved for the OWNER. A member may edit the books
    and archive an entity (the safe, reversible default), but not irrecover-
    ably destroy one: that is the household-control-losing, irreversible act
    the role design keeps with the owner. The typed-name confirmation is a
    mistake-guard, not an authorization control — every member can read the
    name — so it cannot substitute for the role gate.
    """

    @classmethod
    def setUpClass(cls):
        import os
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.app = appmod.app

    def setUp(self):
        from oikonome.web import security
        security._limiter._hits.clear()

    def _owner(self):
        from fastapi.testclient import TestClient
        c = TestClient(self.app)
        r = c.post("/api/signup", data={
            "email": f"biz-owner-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "correct-horse-battery"})
        self.assertEqual(r.status_code, 200, r.text)
        return c

    def _member_of(self, owner):
        """Invite a MEMBER (not the default viewer) into the owner's
        household and return that member's signed-in client."""
        from fastapi.testclient import TestClient
        inv = owner.post("/api/invites", json={
            "label": "bookkeeper", "role": "member",
            "password": "correct-horse-battery"})
        self.assertEqual(inv.status_code, 200, inv.text)
        token = inv.json()["url"].rsplit("token=", 1)[1]
        member = TestClient(self.app)
        r = member.post("/api/invite/claim", json={
            "token": token,
            "email": f"biz-member-{uuid.uuid4().hex[:8]}@example.dev",
            "password": "member-pass-12345"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(member.get("/api/me").json()["role"], "member")
        return member

    def _entity(self, owner, name="Owner Gate LLC"):
        r = owner.post("/api/business/entities", json={
            "name": name, "structure": "sole_prop"})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def test_member_cannot_delete_forever_even_with_the_exact_name(self):
        owner = self._owner()
        eid = self._entity(owner)
        member = self._member_of(owner)
        # the member can SEE the entity (household shares data) and knows its
        # name — the typed-name guard is no barrier to them at all
        listed = member.get("/api/business/entities").json()["entities"]
        self.assertIn(eid, [e["id"] for e in listed])
        # exact name, correct entity — the refusal must be the ROLE
        r = member.post(f"/api/business/entities/{eid}/delete-forever",
                        json={"name": "Owner Gate LLC"})
        self.assertEqual(r.status_code, 403, r.text)
        # nothing was destroyed
        listed = owner.get("/api/business/entities").json()["entities"]
        self.assertIn(eid, [e["id"] for e in listed])

    def test_member_may_still_archive_the_entity(self):
        # the reversible default stays a member capability — the gate is on
        # destruction, not on removing a business from active use
        owner = self._owner()
        eid = self._entity(owner, "Archivable LLC")
        member = self._member_of(owner)
        r = member.post(f"/api/business/entities/{eid}/archive")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "archived")

    def test_owner_can_still_delete_forever(self):
        owner = self._owner()
        eid = self._entity(owner, "Owner Destroys LLC")
        r = owner.post(f"/api/business/entities/{eid}/delete-forever",
                       json={"name": "Owner Destroys LLC"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("destroyed", r.json())
        listed = owner.get("/api/business/entities").json()["entities"]
        self.assertNotIn(eid, [e["id"] for e in listed])


if __name__ == "__main__":
    unittest.main()
