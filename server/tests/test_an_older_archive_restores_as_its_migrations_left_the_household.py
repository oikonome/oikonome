"""An archive written before a migration that rewrote stored rows restores
in the shape that migration left the household's own rows in.

A migration changes the rows already in the database and nothing else, so
an archive taken before it brings the old shape back unless the restore
applies the same change on the way in — and a restore never runs a
migration's file over archived text. Each family below is an archive in the
shape an install before the migration wrote, with invented data, restored
into a fresh household; and an archive the current install writes restores
exactly as it did, because every step is the rule the ingest applies to a
new row and so changes nothing on a row that already follows it.
"""

import datetime as dt
import io
import json
import os
import unittest
import uuid
import zipfile
from unittest import mock

from oikonome.db import tenancy
from oikonome.notify import recipient_invites
from oikonome.sync import archive_upgrades, export, restore

from .util import TEST_DB, _admin_dsn, add_txn, make_db, write_config


def _zip(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buf.getvalue()


def _cell(v) -> str:
    v = v if isinstance(v, str) else json.dumps(v)
    return '"' + v.replace('"', '""') + '"'


def _csv(header: list[str], rows: list[list]) -> str:
    lines = [",".join(header)]
    for r in rows:
        lines.append(",".join("" if v is None else _cell(v) for v in r))
    return "\n".join(lines) + "\n"


def _manifest(schema: str) -> str:
    return archive_upgrades.manifest(schema, "test", [])


def _tid(conn) -> str:
    return conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS t").fetchone()["t"]


_OLD_TXN = ["id", "account_id", "date", "amount", "name", "merchant_name",
            "category_primary", "category_detailed", "category_override",
            "pending", "removed", "raw"]


class TransactionRowsTests(unittest.TestCase):
    """064, 075, 081-083, 091-094, 098: what a transaction row of an archive
    from before them is rewritten to."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _restore(self, rows: list[list]) -> dict:
        restore.restore_zip(self.conn, _zip(
            {"transactions.csv": _csv(_OLD_TXN, rows)}))
        return {r["id"]: r for r in self.conn.execute(
            "SELECT * FROM transactions").fetchall()}

    def test_plaids_category_and_facts_come_out_of_the_raw_record(self):
        raw = {"personal_finance_category": {
                   "primary": "FOOD_AND_DRINK",
                   "detailed": "FOOD_AND_DRINK_COFFEE",
                   "confidence_level": "HIGH"},
               "check_number": "1042", "payment_channel": "in store",
               "merchant_category_code": "5814",
               "authorized_date": "2026-03-02",
               "counterparties": [{"name": "Tapline Pay",
                                   "type": "payment_terminal"}],
               "location": {"city": "Riverton", "region": "ZZ",
                            "address": "1 Example Way",
                            "postal_code": "00000", "lat": 1.5, "lon": 2.5,
                            "store_number": "77"}}
        got = self._restore([["pfc-1", "chk", "2026-03-03", "4.5",
                              "CEDAR COFFEE", "Cedar Coffee",
                              "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE",
                              None, 0, 0, raw]])["pfc-1"]
        self.assertEqual((got["category_plaid"], got["category_plaid_detailed"],
                          got["category_plaid_confidence"]),
                         ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "HIGH"))
        self.assertEqual(got["category_source"], "plaid")
        self.assertEqual(got["check_number"], "1042")
        self.assertEqual(got["payment_channel"], "in store")
        self.assertEqual(got["payment_processor"], "Tapline Pay")
        self.assertEqual(got["mcc"], "5814")
        self.assertEqual(got["authorized_date"], dt.date(2026, 3, 2))
        self.assertEqual((got["location_city"], got["location_region"],
                          got["location_address"], got["location_postal"],
                          got["location_lat"], got["location_lon"],
                          got["location_store"]),
                         ("Riverton", "ZZ", "1 Example Way", "00000", 1.5,
                          2.5, "77"))

    def test_a_coordinate_no_float_can_hold_lands_as_none(self):
        """A raw record is untrusted: an integer of hundreds of digits is
        no coordinate, nor is one off the globe, and neither may fail the
        restore of every other row."""
        got = self._restore([
            ["huge", "chk", "2026-03-03", "4.5", "CEDAR COFFEE", None,
             None, None, None, 0, 0,
             {"location": {"city": "Riverton", "lat": 10 ** 400,
                           "lon": 2.5}}],
            ["off", "chk", "2026-03-04", "4.5", "CEDAR COFFEE", None,
             None, None, None, 0, 0,
             {"location": {"lat": 91.0, "lon": -181.0}}]])
        self.assertEqual((got["huge"]["location_lat"],
                          got["huge"]["location_lon"],
                          got["huge"]["location_city"]),
                         (None, 2.5, "Riverton"))
        self.assertEqual((got["off"]["location_lat"],
                          got["off"]["location_lon"]), (None, None))

    def test_who_decided_a_category_is_derived_as_the_migration_did(self):
        got = self._restore([
            ["src-rule", "chk", "2026-03-03", "9", "X", "Shop One",
             "SHOPPING", None, None, 0, 0,
             {"personal_finance_category": {"primary": "GENERAL_SERVICES"}}],
            ["src-flow", "chk", "2026-03-03", "9", "MOVE", None,
             "TRANSFER_OUT", None, None, 0, 0, {}],
            ["src-import", "chk", "2026-03-03", "9", "SHOP", None,
             "SHOPPING", None, None, 0, 0, {}],
            ["src-none", "chk", "2026-03-03", "9", "SHOP", None,
             None, None, None, 0, 0, {}]])
        self.assertEqual({k: v["category_source"] for k, v in got.items()},
                         {"src-rule": "rule", "src-flow": "flow",
                          "src-import": "import", "src-none": None})

    def test_a_card_payment_the_aggregator_mislabelled_is_one(self):
        got = self._restore([
            ["pay-1", "card", "2026-03-03", "-120", "PAYMENT THANK YOU - WEB",
             None, "LOAN_DISBURSEMENTS",
             "LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT", None, 0, 0, {}],
            # a refund on the card names no payment, and stays one
            ["refund-1", "card", "2026-03-03", "-20", "HARBOR HARDWARE",
             None, "GENERAL_MERCHANDISE", None, None, 0, 0, {}],
            # on checking a payment word could be any merchant
            ["chk-1", "chk", "2026-03-03", "-120", "PAYMENT THANK YOU",
             None, "LOAN_DISBURSEMENTS", None, None, 0, 0, {}]])
        self.assertEqual((got["pay-1"]["category_primary"],
                          got["pay-1"]["category_detailed"],
                          got["pay-1"]["category_source"]),
                         ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
                          "flow"))
        self.assertEqual(got["refund-1"]["category_primary"],
                         "GENERAL_MERCHANDISE")
        self.assertEqual(got["chk-1"]["category_primary"],
                         "LOAN_DISBURSEMENTS")

    def test_venmo_and_zelle_rows_take_their_counterparty(self):
        venmo = {"original_description":
                 "VENMO *Taylor Sample NEW YORK, NY, US"}
        got = self._restore([
            # the aggregator left it unnamed (081, with the rail of 083)
            ["v-1", "chk", "2026-03-03", "25", "Venmo", None, "TRANSFER_OUT",
             None, None, 0, 0, venmo],
            # 081 already named it, bare (083)
            ["v-2", "chk", "2026-03-04", "25", "Venmo", "Taylor Sample",
             "TRANSFER_OUT", None, None, 0, 0, venmo],
            # money out names the "to" side (082)
            ["z-1", "chk", "2026-03-05", "40",
             "Zelle payment from Sam Owner (Acme Checking XXXXXX1234) to "
             "CASEY EXAMPLE", None, "TRANSFER_OUT", None, None, 0, 0, {}],
            # a merchant the aggregator really named is kept
            ["v-3", "chk", "2026-03-06", "25", "Venmo", "Cedar Coffee",
             "FOOD_AND_DRINK", None, None, 0, 0, venmo]])
        self.assertEqual(got["v-1"]["merchant_name"], "Venmo — Taylor Sample")
        self.assertEqual(got["v-2"]["merchant_name"], "Venmo — Taylor Sample")
        self.assertEqual(got["z-1"]["merchant_name"], "Zelle — Casey Example")
        self.assertEqual(got["v-3"]["merchant_name"], "Cedar Coffee")

    def test_a_pump_is_its_own_outlet_and_is_filed_as_fuel(self):
        raw = {"merchant_name": "Northwind Club",
               "original_description": "NORTHWIND CLUB GAS #1234"}
        got = self._restore([
            # 091 wrote the suffix into merchant_name
            ["f-1", "card", "2026-03-03", "48", "Northwind Club Gas",
             "Northwind Club Gas", "FOOD_AND_DRINK",
             "FOOD_AND_DRINK_GROCERIES", None, 0, 0, raw],
            # the person's own category is never read or written
            ["f-2", "card", "2026-03-04", "48", "Northwind Club Gas",
             "Northwind Club", "FOOD_AND_DRINK", None, "Groceries", 0, 0,
             raw],
            # the warehouse is not the pump
            ["w-1", "card", "2026-03-05", "180", "Northwind Club",
             "Northwind Club", "FOOD_AND_DRINK", None, None, 0, 0,
             {"merchant_name": "Northwind Club",
              "original_description": "NORTHWIND CLUB WHSE #1234"}],
            # a row the aggregator withdrew keeps what it had
            ["f-3", "card", "2026-03-06", "48", "Northwind Club Gas",
             "Northwind Club Gas", "FOOD_AND_DRINK", None, None, 0, 1, raw]])
        f1 = got["f-1"]
        self.assertEqual((f1["merchant_name"], f1["merchant_outlet"]),
                         ("Northwind Club", "Northwind Club Gas"))
        self.assertEqual((f1["category_primary"], f1["category_detailed"],
                          f1["category_source"]),
                         ("TRANSPORTATION", "TRANSPORTATION_GAS", "fuel"))
        f2 = got["f-2"]
        self.assertEqual(f2["merchant_outlet"], "Northwind Club Gas")
        self.assertEqual((f2["category_primary"], f2["category_override"]),
                         ("FOOD_AND_DRINK", "Groceries"))
        self.assertIsNone(got["w-1"]["merchant_outlet"])
        self.assertEqual(got["w-1"]["category_primary"], "FOOD_AND_DRINK")
        self.assertEqual((got["f-3"]["merchant_name"],
                          got["f-3"]["merchant_outlet"],
                          got["f-3"]["category_primary"]),
                         ("Northwind Club Gas", None, "FOOD_AND_DRINK"))

    def test_a_category_a_rule_decided_is_left_as_the_rule_left_it(self):
        """The current shape: an outlet on a row a person's merchant rule
        filed elsewhere. The fuel stamp is the ingest's, and the rule
        re-stamps over it; the restore keeps the rule's answer."""
        header = _OLD_TXN + ["merchant_outlet", "category_source"]
        restore.restore_zip(self.conn, _zip({"transactions.csv": _csv(
            header, [["r-1", "card", "2026-03-03", "48", "Northwind Club Gas",
                      "Northwind Club", "FOOD_AND_DRINK", None, None, 0, 0,
                      {"original_description": "NORTHWIND CLUB GAS #1"},
                      "Northwind Club Gas", "rule_user"]])}))
        got = self.conn.execute(
            "SELECT category_primary, category_source FROM transactions"
            " WHERE id = 'r-1'").fetchone()
        self.assertEqual((got["category_primary"], got["category_source"]),
                         ("FOOD_AND_DRINK", "rule_user"))


class CurrentArchiveRoundTripTests(unittest.TestCase):
    """An archive the current install writes restores as it did: every
    transaction column the source holds lands unchanged."""

    _COLS = ("name", "merchant_name", "merchant_outlet", "category_primary",
             "category_detailed", "category_plaid", "category_source",
             "check_number", "payment_processor", "mcc", "authorized_date")

    def test_every_upgraded_column_round_trips_unchanged(self):
        src = make_db()
        self.addCleanup(src.close)
        add_txn(src, dt.date(2026, 3, 3), 48, "Northwind Club Gas",
                txn_id="rt-1")
        add_txn(src, dt.date(2026, 3, 4), -120, "PAYMENT THANK YOU",
                txn_id="rt-2")
        src.execute(
            "UPDATE transactions SET merchant_name = 'Northwind Club',"
            " merchant_outlet = 'Northwind Club Gas',"
            " category_primary = 'FOOD_AND_DRINK', category_source = 'rule_llm',"
            " raw = %s WHERE id = 'rt-1'",
            (json.dumps({"original_description": "NORTHWIND CLUB GAS #9",
                         "check_number": "7"}),))
        # as the ingest writes it: the fact is in its column as in raw
        src.execute("UPDATE transactions SET check_number = '7'"
                    " WHERE id = 'rt-1'")
        src.execute(
            "UPDATE transactions SET account_id = 'card',"
            " category_primary = 'INCOME', category_source = 'rule_user'"
            " WHERE id = 'rt-2'")
        before = {r["id"]: tuple(r[c] for c in self._COLS) for r in
                  src.execute("SELECT * FROM transactions").fetchall()}
        dest = make_db()
        self.addCleanup(dest.close)
        restore.restore_zip(dest, export.build_zip(src))
        after = {r["id"]: tuple(r[c] for c in self._COLS) for r in
                 dest.execute("SELECT * FROM transactions").fetchall()}
        self.assertEqual(after, before)


class ConnectionsTests(unittest.TestCase):
    """022: a push door's connection an old archive calls 'csv'."""

    def test_push_door_connections_land_as_their_door_and_archived(self):
        conn = make_db()
        self.addCleanup(conn.close)
        restore.restore_zip(conn, _zip({
            "items.csv": _csv(["id", "aggregator", "institution_name",
                               "status"],
                              [["coinbase-main", "csv", "Coinbase", "ok"],
                               ["plan", "csv", "Workplace plan", "ok"],
                               ["bank-file", "csv", "Statement", "ok"]]),
            "accounts.csv": _csv(["id", "item_id", "name", "type"],
                                 [["plan-401k", "plan", "Plan", "investment"],
                                  ["bank-file-chk", "bank-file", "Chk",
                                   "depository"]])}))
        got = {r["id"]: (r["aggregator"], r["status"]) for r in conn.execute(
            "SELECT id, aggregator, status FROM items"
            " WHERE id <> 'it1'").fetchall()}
        self.assertEqual(got, {"coinbase-main": ("coinbase", "archived"),
                               "plan": ("plan_csv", "archived"),
                               "bank-file": ("csv", "restored")})


class BusinessTests(unittest.TestCase):
    """089 (an archived business's date) and 164 (a 1099 mark's merchant)."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.ent = str(uuid.uuid4())
        self.closed = str(uuid.uuid4())

    def _restore(self, extra: dict) -> None:
        restore.restore_zip(self.conn, _zip({
            "business_entity.csv": _csv(
                ["id", "name", "structure", "status", "updated_at"],
                [[self.ent, "Example Works", "sole_prop", "active",
                  "2025-01-02T03:04:05+00:00"],
                 [self.closed, "Old Works", "sole_prop", "archived",
                  "2024-06-07T08:09:10+00:00"]]),
            **extra}))

    def test_a_business_closed_before_the_date_was_kept_is_dated(self):
        self._restore({})
        got = {str(r["id"]): r["archived_at"] for r in self.conn.execute(
            "SELECT id, archived_at FROM business_entity").fetchall()}
        self.assertIsNone(got[self.ent])
        self.assertEqual(got[self.closed],
                         dt.datetime(2024, 6, 7, 8, 9, 10,
                                     tzinfo=dt.timezone.utc))

    def test_a_1099_mark_follows_the_one_merchant_of_its_name(self):
        builder, twin_a, twin_b = (str(uuid.uuid4()) for _ in range(3))
        self._restore({
            "merchants.csv": _csv(["id", "name", "name_source"],
                                  [[builder, "Pat Builder", "manual"],
                                   [twin_a, "Twin Name", "manual"],
                                   [twin_b, "Twin Name", "layer1"]]),
            "vendor_1099.csv": _csv(
                ["id", "entity_id", "merchant", "reportable"],
                [[str(uuid.uuid4()), self.ent, "Pat Builder", "true"],
                 [str(uuid.uuid4()), self.ent, "Twin Name", "true"],
                 [str(uuid.uuid4()), self.ent, "Nobody Here", "true"]])})
        got = {r["merchant"]: r["merchant_id"] and str(r["merchant_id"])
               for r in self.conn.execute(
                   "SELECT merchant, merchant_id FROM vendor_1099").fetchall()}
        self.assertEqual(got, {"Pat Builder": builder, "Twin Name": None,
                               "Nobody Here": None})


class CategoryRulesTests(unittest.TestCase):
    """035 (rules from the person's corrections) and 044 (model rules on
    the merchant), for an archive that says it is older than them."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)

    def _archive(self, schema: str | None) -> bytes:
        files = {
            "transactions.csv": _csv(_OLD_TXN, [
                ["c-1", "chk", "2026-03-01", "4", "CEDAR COFFEE 1",
                 "Cedar Coffee", "FOOD_AND_DRINK", None, "Treats", 0, 0, {}],
                ["c-2", "chk", "2026-03-02", "4", "CEDAR COFFEE 2",
                 "Cedar Coffee", "FOOD_AND_DRINK", None, "Treats", 0, 0, {}],
                # corrections that disagree derive nothing
                ["d-1", "chk", "2026-03-01", "9", "DUNE DELI", "Dune Deli",
                 "FOOD_AND_DRINK", None, "Lunch", 0, 0, {}],
                ["d-2", "chk", "2026-03-02", "9", "DUNE DELI", "Dune Deli",
                 "FOOD_AND_DRINK", None, "Treats", 0, 0, {}]]),
            "manual_categories.csv": _csv(
                ["transaction_id", "category"],
                [["c-1", "Treats"], ["c-2", "Treats"], ["d-1", "Lunch"],
                 ["d-2", "Treats"]]),
            "merchant_canonical.csv": _csv(
                ["raw_merchant", "canonical", "method"],
                [["SQ *ELM BAKERY", "Elm Bakery", "manual"],
                 ["ELM BAKERY #2", "Elm Bakery", "manual"]]),
            "merchant_categories.csv": _csv(
                ["merchant", "category_primary", "source"],
                [["SQ *ELM BAKERY", "FOOD_AND_DRINK", "llm"],
                 ["ELM BAKERY #2", "FOOD_AND_DRINK", "llm"]])}
        if schema:
            files[archive_upgrades.MANIFEST] = _manifest(schema)
        return _zip(files)

    def _rules(self) -> dict:
        return {r["merchant"]: (r["category_primary"], r["source"])
                for r in self.conn.execute(
                    "SELECT merchant, category_primary, source"
                    " FROM merchant_categories").fetchall()}

    def test_an_old_archive_gets_the_rules_its_migrations_derived(self):
        restore.restore_zip(self.conn, self._archive(None))
        rules = self._rules()
        self.assertEqual(rules.get("Cedar Coffee"), ("Treats", "user"))
        self.assertNotIn("Dune Deli", rules)
        self.assertEqual(rules.get("Elm Bakery"), ("FOOD_AND_DRINK", "llm"))
        self.assertNotIn("SQ *ELM BAKERY", rules)
        self.assertNotIn("ELM BAKERY #2", rules)

    def test_a_current_archive_keeps_its_rules_as_it_wrote_them(self):
        restore.restore_zip(self.conn, self._archive(
            archive_upgrades.migration_names()[-1]))
        rules = self._rules()
        self.assertNotIn("Cedar Coffee", rules)
        self.assertIn("SQ *ELM BAKERY", rules)
        self.assertNotIn("Elm Bakery", rules)

    def _without_rules(self):
        rules = self._rules()
        self.assertNotIn("Cedar Coffee", rules)
        self.assertIn("SQ *ELM BAKERY", rules)
        self.assertNotIn("Elm Bakery", rules)

    def test_an_archive_without_a_manifest_is_as_new_as_its_columns(self):
        """Every archive exported before the manifest existed has none; one
        whose rows carry a column a migration after 035 and 044 added was
        written after both, and is not given them again."""
        data = self._archive(None)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = {n: z.read(n).decode() for n in z.namelist()}
        head, _, body = files["transactions.csv"].partition("\n")
        files["transactions.csv"] = (
            head + ",merchant_outlet\n"
            + "".join(line + ",\n" for line in body.splitlines()))
        self.assertEqual(archive_upgrades.archive_schema(zipfile.ZipFile(
            io.BytesIO(_zip(files)))), "092_merchant_outlet.sql")
        restore.restore_zip(self.conn, _zip(files))
        self._without_rules()

    def test_a_file_whose_lines_end_in_a_bare_return_is_read_by_its_header(
            self):
        """A CSV saved with carriage returns alone (the old Mac line end)
        is one the row reader parses; the age read must parse it too, not
        raise inside the restore and roll the whole restore back."""
        data = self._archive(None)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = {n: z.read(n).decode() for n in z.namelist()}
        head, _, body = files["transactions.csv"].partition("\n")
        files["transactions.csv"] = "\r".join(
            [head + ",merchant_outlet"]
            + [line + "," for line in body.splitlines()]) + "\r"
        self.assertEqual(archive_upgrades.archive_schema(zipfile.ZipFile(
            io.BytesIO(_zip(files)))), "092_merchant_outlet.sql")
        restore.restore_zip(self.conn, _zip(files))
        self._without_rules()
        self.assertEqual(self.conn.execute(
            "SELECT count(*) AS n FROM transactions").fetchone()["n"], 4)

    def test_an_archive_from_a_newer_install_is_not_read_as_the_oldest(
            self):
        restore.restore_zip(self.conn, self._archive(
            "999_a_migration_this_install_does_not_have.sql"))
        self._without_rules()


def _admin():
    return tenancy.admin_connect(_admin_dsn(TEST_DB))


class RecipientsFromBeforeInvitesTests(unittest.TestCase):
    """074: an archive from before recipient invites carries no answers,
    and the household it came from was mailing its recipients."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        write_config(self.conn)
        self.tid = _tid(self.conn)
        self.member = f"member-{uuid.uuid4().hex[:8]}@example.test"
        self.outsider = f"outsider-{uuid.uuid4().hex[:8]}@example.test"
        admin = _admin()
        try:
            admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash, role,"
                " verified_at) VALUES (%s, %s, 'x', 'member', now())",
                (self.tid, self.member))
        finally:
            admin.close()

    def _archive(self, *, with_answers: bool) -> bytes:
        files = {"tenant_settings.csv": "config\n" + _cell(json.dumps(
            {"email_recipients": [self.member, self.outsider]})) + "\n"}
        if with_answers:
            files["recipient_invites.csv"] = (
                "email,invited_by_email,created_at,expires_at,accepted_at,"
                "declined_at,last_sent_at,send_count\r\n")
        return _zip(files)

    def test_the_households_own_members_keep_receiving_the_mail(self):
        counts = restore.restore_zip(self.conn,
                                     self._archive(with_answers=False))
        self.assertEqual(
            recipient_invites.accepted(self.tid, [self.member]),
            {self.member})
        admin = _admin()
        try:
            row = admin.execute(
                "SELECT token_hash, accepted_at FROM recipient_invites"
                " WHERE tenant_id = %s AND email = %s",
                (self.tid, self.member)).fetchone()
        finally:
            admin.close()
        self.assertIsNone(row["token_hash"],
                          "consent from before invites never had a link")
        note = " ".join(counts.get("_notes") or [])
        self.assertIn("the archive listed 2 recipient(s)", note)
        self.assertIn("1 must accept a fresh invite", note)

    def test_an_archive_without_answers_never_enrols_an_outsider(self):
        """Stripping the answers out of an archive is all it takes to make
        one look this old, so it must not be a way to enrol a mailbox."""
        restore.restore_zip(self.conn, self._archive(with_answers=False))
        self.assertEqual(
            recipient_invites.accepted(self.tid, [self.outsider]), set())

    def test_on_hosted_the_member_must_have_proven_the_mailbox_here(self):
        admin = _admin()
        try:
            admin.execute("UPDATE users SET verified_at = NULL"
                          " WHERE tenant_id = %s AND email = %s",
                          (self.tid, self.member))
        finally:
            admin.close()
        with mock.patch.dict(os.environ, {"OIKONOME_HOSTED": "1"}):
            restore.restore_zip(self.conn, self._archive(with_answers=False))
            self.assertEqual(
                recipient_invites.accepted(self.tid, [self.member]), set())

    def test_an_archive_written_after_invites_enrols_nobody(self):
        """An export from after 074 but before the export carried the
        answers lacks the member too; its rows' columns say it is newer
        than 074, so its silence is not consent."""
        data = self._archive(with_answers=False)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = {n: z.read(n).decode() for n in z.namelist()}
        files["transactions.csv"] = _csv(_OLD_TXN + ["merchant_outlet"], [])
        counts = restore.restore_zip(self.conn, _zip(files))
        self.assertEqual(
            recipient_invites.accepted(self.tid, [self.member]), set())
        # Settings still lists both and the worker mails neither, so the
        # restore has to say so rather than let the mail stop in silence
        note = " ".join(counts.get("_notes") or [])
        self.assertIn("the archive listed 2 recipient(s)", note)
        self.assertIn("2 must accept a fresh invite", note)

    def test_a_recipient_the_answers_leave_out_is_named_as_waiting(self):
        """An archive whose answers member never names a configured
        recipient enrols nobody, and says that recipient must be invited."""
        counts = restore.restore_zip(self.conn,
                                     self._archive(with_answers=True))
        note = " ".join(counts.get("_notes") or [])
        self.assertIn("2 must accept a fresh invite", note)

    def test_an_archive_with_answers_is_read_for_them_alone(self):
        """An archive that carries the answers member and names nobody in
        it says nobody was asked: nobody here answers for them."""
        restore.restore_zip(self.conn, self._archive(with_answers=True))
        self.assertEqual(
            recipient_invites.accepted(self.tid, [self.member]), set())


class FileRowStandInNoteTests(unittest.TestCase):
    """146: a file row the app noted a feed row against is still a file
    row, and its identifier columns are cut."""

    def test_a_noted_file_row_has_its_identifier_columns_cut(self):
        raw = {"Account Number": "000123456789", "Description": "ROW",
               "_stood_in": {"source": "plaid", "accounts": ["chk"]}}
        out = restore.scrub_transaction_raw(raw)
        self.assertEqual(out["Account Number"], "6789")
        self.assertEqual(out["_stood_in"], raw["_stood_in"])


if __name__ == "__main__":
    unittest.main()
