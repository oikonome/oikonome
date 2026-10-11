"""A reconnect only drops what the adopted account already holds, and only
adopts from connections that are really dead.

The reconnect guard drops a backfilled row on its date alone when it is
well before the cutoff, because the restore already has it. That is true
only inside the span the restored history covers. A connection linked a few
months before the backup carries a few months, while the fresh link
replays up to two years; a row older than anything the account brought
with it has no copy here, and dropping it loses it for good because the
cursor moves on.

And adoption takes accounts only from a connection with nothing left to
sync. A SimpleFIN bank and an MX member hold no token even while they work
(the bridge or the MX user does), so a missing token is no evidence there:
adopting one would move a live feed onto the new link and archive it.
"""

import datetime as dt
import unittest

from oikonome.sync import adopt

from .util import make_db, write_config


def _item(conn, iid, aggregator, status, *, inst_id=None, inst_name=None,
          token=None):
    conn.execute(
        "INSERT INTO items (id, aggregator, institution_id, "
        "institution_name, status, access_token) "
        "VALUES (%s,%s,%s,%s,%s,%s)",
        (iid, aggregator, inst_id, inst_name, status, token))


def _account(conn, aid, iid, *, mask, type_="depository", name="Checking"):
    conn.execute(
        "INSERT INTO accounts (id, item_id, name, type, mask) "
        "VALUES (%s,%s,%s,%s,%s)", (aid, iid, name, type_, mask))


def _txn(conn, tid, aid, date, amount, name="COFFEE"):
    conn.execute(
        "INSERT INTO transactions (id, account_id, date, amount, name, "
        "pending, removed) VALUES (%s,%s,%s,%s,%s,0,0)",
        (tid, aid, date, amount, name))


class _Txn:
    def __init__(self, account_id, date, amount, name, id=None):
        self.account_id, self.date = account_id, date
        self.amount, self.name = amount, name
        self.merchant_name = None
        self.id = id


class BackfillOlderThanTheRestore(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "plaid", "restored", inst_id="ins_1",
              inst_name="Acme")
        _account(self.conn, "old-acct", "old-item", mask="1234")
        # the restored history runs from January to June only
        _txn(self.conn, "old-1", "old-acct", dt.date(2026, 1, 5), 10.0)
        _txn(self.conn, "old-2", "old-acct", dt.date(2026, 6, 1), 20.0,
             "MARKET STORE 12")
        _item(self.conn, "new-item", "plaid", "ok", inst_id="ins_1",
              inst_name="Acme", token="live-token")
        adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_1",
            institution_name="Acme",
            incoming=[{"account_id": "new-acct", "mask": "1234",
                       "type": "depository"}])

    def test_the_adoption_records_where_the_history_starts(self):
        row = self.conn.execute(
            "SELECT cutoff_date, history_from FROM account_adoptions "
            "WHERE account_id='new-acct'").fetchone()
        self.assertEqual(row["cutoff_date"], dt.date(2026, 6, 1))
        self.assertEqual(row["history_from"], dt.date(2026, 1, 5))

    def test_a_row_older_than_the_restored_history_is_kept(self):
        older = _Txn("new-acct", dt.date(2025, 3, 1), 42.0, "HARDWARE",
                     id="p-older")
        held = _Txn("new-acct", dt.date(2026, 3, 3), 5.0, "BOOKS",
                    id="p-held")
        kept, stats = adopt.filter_incoming(self.conn, "new-acct",
                                            [older, held])
        self.assertEqual([t.id for t in kept], ["p-older"])
        self.assertEqual(stats["dropped_before_cutoff"], 1)
        self.assertEqual(stats["kept_before_history"], 1)

    def test_a_row_at_the_floor_is_matched_not_dropped_on_its_date(self):
        # two days before the oldest restored row: within the matcher's
        # reach of it, so the restored COFFEE claims the re-delivered one
        # and a different charge on the same day is kept
        dup = _Txn("new-acct", dt.date(2026, 1, 3), 10.0, "COFFEE",
                   id="p-dup")
        fresh = _Txn("new-acct", dt.date(2026, 1, 3), 61.0, "GARAGE",
                     id="p-fresh")
        kept, stats = adopt.filter_incoming(self.conn, "new-acct",
                                            [dup, fresh])
        self.assertEqual([t.id for t in kept], ["p-fresh"])
        self.assertEqual(stats["deduped_overlap"], 1)

    def test_an_adoption_recorded_without_the_floor_keeps_the_old_rule(self):
        self.conn.execute("UPDATE account_adoptions SET history_from = NULL")
        older = _Txn("new-acct", dt.date(2025, 3, 1), 42.0, "HARDWARE",
                     id="p-older")
        kept, stats = adopt.filter_incoming(self.conn, "new-acct", [older])
        self.assertEqual(kept, [])
        self.assertEqual(stats["dropped_before_cutoff"], 1)


class LiveTokenlessFeedsAreNotDonors(unittest.TestCase):

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "new-item", "plaid", "ok", inst_id="ins_7",
              inst_name="Harbor Bank", token="live-token")

    def _adopt(self):
        return adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_7",
            institution_name="Harbor Bank",
            incoming=[{"account_id": "plaid-acct", "mask": "1234",
                       "type": "depository"}])

    def _simplefin(self, *, bridge_status="ok", bridge_token="https://b",
                   child_status="ok"):
        _item(self.conn, "sfin", "simplefin", bridge_status,
              inst_name="SimpleFIN bridge", token=bridge_token)
        _item(self.conn, "sfin:harbor-bank", "simplefin-org", child_status,
              inst_name="Harbor Bank")
        _account(self.conn, "sfin-acct", "sfin:harbor-bank", mask="1234",
                 name="Spending Account (1234)")

    def _status(self, iid):
        return self.conn.execute("SELECT status FROM items WHERE id=%s",
                                 (iid,)).fetchone()["status"]

    def test_a_live_simplefin_bank_keeps_its_account(self):
        self._simplefin()
        self.assertEqual(self._adopt(), [])
        self.assertTrue(self.conn.execute(
            "SELECT 1 FROM accounts WHERE id='sfin-acct' "
            "AND item_id='sfin:harbor-bank'").fetchone())
        self.assertEqual(self._status("sfin:harbor-bank"), "ok")

    def test_a_disconnected_simplefin_bank_is_adopted(self):
        self._simplefin(child_status="archived")
        self.assertEqual(len(self._adopt()), 1)

    def test_a_bank_whose_bridge_is_gone_is_adopted(self):
        self._simplefin(bridge_status="archived", bridge_token=None)
        self.assertEqual(len(self._adopt()), 1)

    def _mx(self, status="ok"):
        _item(self.conn, "mx-member-1", "mx", status,
              inst_name="Harbor Bank")
        _account(self.conn, "mx-acct", "mx-member-1", mask="1234")

    def test_a_live_mx_member_keeps_its_account(self):
        write_config(self.conn, mx_client_id="client", mx_api_key="key",
                     mx_user_guid="USR-1")
        self._mx()
        self.assertEqual(self._adopt(), [])
        self.assertEqual(self._status("mx-member-1"), "ok")

    def test_a_disconnected_mx_member_is_adopted(self):
        write_config(self.conn, mx_client_id="client", mx_api_key="key",
                     mx_user_guid="USR-1")
        self._mx(status="archived")
        self.assertEqual(len(self._adopt()), 1)

    def test_an_mx_member_with_no_mx_user_left_is_adopted(self):
        self._mx()
        self.assertEqual(len(self._adopt()), 1)

    def test_a_manual_account_is_never_a_donor(self):
        _item(self.conn, "manual-harbor", "manual", "restored",
              inst_name="Harbor Bank")
        _account(self.conn, "manual:harbor", "manual-harbor", mask="1234")
        self.assertEqual(self._adopt(), [])


class FileHistoryIsNotTheFeedsFloor(unittest.TestCase):
    """A card holding a statement import from years back and a few months
    of feed rows: the backfill's months between the two are held by
    nothing, so the floor is where the feed's rows start."""

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        _item(self.conn, "old-item", "plaid", "restored", inst_id="ins_2",
              inst_name="Acme")
        _account(self.conn, "old-acct", "old-item", mask="4321")
        _txn(self.conn, "csv:old-file-row", "old-acct",
             dt.date(2019, 6, 1), 30.0, "BOOKSHOP")
        _txn(self.conn, "feed-1", "old-acct", dt.date(2026, 4, 2), 10.0)
        _txn(self.conn, "feed-2", "old-acct", dt.date(2026, 7, 1), 20.0)
        _item(self.conn, "new-item", "plaid", "ok", inst_id="ins_2",
              inst_name="Acme", token="live-token")
        adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_2",
            institution_name="Acme",
            incoming=[{"account_id": "new-acct", "mask": "4321",
                       "type": "depository"}])

    def test_the_floor_ignores_file_rows(self):
        row = self.conn.execute(
            "SELECT cutoff_date, history_from FROM account_adoptions "
            "WHERE account_id='new-acct'").fetchone()
        self.assertEqual(row["history_from"], dt.date(2026, 4, 2))
        self.assertEqual(row["cutoff_date"], dt.date(2026, 7, 1))

    def test_a_row_in_the_gap_is_kept(self):
        gap = _Txn("new-acct", dt.date(2024, 9, 9), 42.0, "HARDWARE",
                   id="p-gap")
        kept, _ = adopt.filter_incoming(self.conn, "new-acct", [gap])
        self.assertEqual([t.id for t in kept], ["p-gap"])

    def test_the_upgrade_moves_an_open_adoptions_floor_to_the_feed(self):
        """An adoption recorded under the any-source rule (the file's
        first row) gets the feed's first row; a closed one is left."""
        from pathlib import Path

        from oikonome.db import tenancy

        from .util import TEST_DB, _admin_dsn
        sql = (Path(adopt.__file__).resolve().parents[1] / "db"
               / "migrations"
               / "167_adoption_floor_is_where_the_feed_begins.sql")
        self.conn.execute("UPDATE account_adoptions SET history_from = %s",
                          (dt.date(2019, 6, 1),))
        admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(admin.close)
        admin.execute(sql.read_text())
        self.assertEqual(self.conn.execute(
            "SELECT history_from FROM account_adoptions "
            "WHERE account_id='new-acct'").fetchone()["history_from"],
            dt.date(2026, 4, 2))
        self.conn.execute("UPDATE account_adoptions SET reconciled_at = now(),"
                          " history_from = %s", (dt.date(2019, 6, 1),))
        admin.execute(sql.read_text())
        self.assertEqual(self.conn.execute(
            "SELECT history_from FROM account_adoptions "
            "WHERE account_id='new-acct'").fetchone()["history_from"],
            dt.date(2019, 6, 1))

    def test_the_upgrade_names_the_same_file_prefixes_as_the_sync(self):
        import re
        from pathlib import Path

        from oikonome.sync import base
        sql = (Path(adopt.__file__).resolve().parents[1] / "db"
               / "migrations"
               / "167_adoption_floor_is_where_the_feed_begins.sql")
        named = re.findall(r"'([a-z]+:)%'", sql.read_text())
        self.assertEqual(sorted(named), sorted(base.FILE_IMPORT_ID_PREFIXES))


class RowsTheNewLinkSentAreNotTheFloor(unittest.TestCase):
    """An adoption still open after the new link's first sync: the rows
    that sync inserted on the account are not restored history, so the
    upgrade's floor never moves on them."""

    def setUp(self):
        from .util import TEST_DB, _admin_dsn
        from oikonome.db import tenancy
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.admin = tenancy.admin_connect(_admin_dsn(TEST_DB))
        self.addCleanup(self.admin.close)
        _item(self.conn, "old-item", "plaid", "restored", inst_id="ins_3",
              inst_name="Acme")
        _account(self.conn, "old-acct", "old-item", mask="7788")
        _txn(self.conn, "csv:first", "old-acct", dt.date(2023, 1, 5), 30.0,
             "BOOKSHOP")
        _txn(self.conn, "csv:last", "old-acct", dt.date(2026, 7, 1), 20.0,
             "BOOKSHOP")
        _item(self.conn, "new-item", "plaid", "ok", inst_id="ins_3",
              inst_name="Acme", token="live-token")
        adopt.adopt_accounts(
            self.conn, item_id="new-item", institution_id="ins_3",
            institution_name="Acme",
            incoming=[{"account_id": "new-acct", "mask": "7788",
                       "type": "depository"}])

    def _sent(self, tid, date, account="new-acct"):
        self.conn.execute(
            "INSERT INTO transactions (id, account_id, date, amount, name, "
            "pending, removed, raw) VALUES (%s,'new-acct',%s,5.0,'KIOSK',0,0,"
            "jsonb_build_object('account_id', %s::text))",
            (tid, date, account))

    def _upgrade(self, name):
        from pathlib import Path
        sql = (Path(adopt.__file__).resolve().parents[1] / "db"
               / "migrations" / name)
        self.admin.execute(sql.read_text())

    def _floor(self):
        return self.conn.execute(
            "SELECT history_from FROM account_adoptions "
            "WHERE account_id='new-acct'").fetchone()["history_from"]

    def test_a_seam_row_the_link_sent_does_not_raise_the_floor(self):
        self.assertEqual(self._floor(), dt.date(2023, 1, 5))
        self._sent("p-seam", dt.date(2026, 6, 20))
        self._upgrade("167_adoption_floor_is_where_the_feed_begins.sql")
        self._upgrade("170_adoption_floor_ignores_what_the_new_link_sent.sql")
        self.assertEqual(self._floor(), dt.date(2023, 1, 5))

    def test_a_kept_older_row_the_link_sent_does_not_lower_it(self):
        self._sent("p-restored", dt.date(2026, 2, 1), account="old-acct")
        self.conn.execute("UPDATE account_adoptions SET history_from = %s",
                          (dt.date(2026, 2, 1),))
        self._sent("p-older", dt.date(2024, 3, 3))
        self._upgrade("167_adoption_floor_is_where_the_feed_begins.sql")
        self._upgrade("170_adoption_floor_ignores_what_the_new_link_sent.sql")
        self.assertEqual(self._floor(), dt.date(2026, 2, 1))

    def test_the_upgrade_names_the_same_file_prefixes_as_the_sync(self):
        import re
        from pathlib import Path

        from oikonome.sync import base
        sql = (Path(adopt.__file__).resolve().parents[1] / "db"
               / "migrations"
               / "170_adoption_floor_ignores_what_the_new_link_sent.sql")
        named = re.findall(r"'([a-z]+:)%'", sql.read_text())
        self.assertEqual(sorted(named), sorted(base.FILE_IMPORT_ID_PREFIXES))


if __name__ == "__main__":
    unittest.main()
