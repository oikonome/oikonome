"""Three writes that must not lose each other's work: the settings
lost-update, the taxdoc row cap, and the email-sweep overlap.
"""
import unittest
from unittest import mock

from oikonome.db import tenancy
from oikonome.engine import taxdocs

from .util import _ensure_db, make_db, write_config


class TaxdocRowCapTests(unittest.TestCase):
    """`rows` comes from the CLIENT (the preview is editable), so its
    length is capped server-side."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_an_absurd_row_count_is_refused(self):
        conn = make_db()
        self.addCleanup(conn.close)
        with self.assertRaises(ValueError) as cm:
            taxdocs.commit(conn, "tok",
                           [{"year": 2020}] * (taxdocs.MAX_ROWS + 1))
        self.assertIn("too many rows", str(cm.exception))

    def test_the_cap_is_checked_before_the_stash_is_popped(self):
        """Order matters: popping first would burn a legitimate preview
        token on a request that is about to be refused anyway."""
        conn = make_db()
        self.addCleanup(conn.close)
        with mock.patch("oikonome.db.staging.pop") as pop:
            with self.assertRaises(ValueError):
                taxdocs.commit(conn, "tok",
                               [{"year": 2020}] * (taxdocs.MAX_ROWS + 1))
        pop.assert_not_called()


class EmailSweepOverlapTests(unittest.TestCase):
    """The due-check and the heartbeat that suppresses it
    are separate statements, so two overlapping sweeps could both send."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def setUp(self):
        self.conn = make_db()
        self.addCleanup(self.conn.close)
        self.tid = str(self.conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(self.conn)

    def test_a_second_sweep_skips_a_tenant_already_being_swept(self):
        from oikonome.jobs import worker
        # hold the lock the way a first, in-flight sweep would
        holder = tenancy.tenant_connect(self.tid)
        self.addCleanup(holder.close)
        got = holder.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                             (f"oikonome:email:{self.tid}",)).fetchone()["ok"]
        self.assertTrue(got)
        with mock.patch.object(worker, "emails_due") as due:
            worker._email_if_due(self.tid)
        due.assert_not_called(
        ) if hasattr(due, "assert_not_called") else None
        self.assertEqual(due.call_count, 0,
                         "a sweep must not even ASK what is due for a tenant "
                         "another sweep is already sending for — that read is "
                         "exactly the half of the race that double-sends")

    def test_the_lock_is_released_so_the_next_hour_still_sends(self):
        from oikonome.jobs import worker
        with mock.patch.object(worker, "emails_due", return_value=[]):
            worker._email_if_due(self.tid)
        probe = tenancy.tenant_connect(self.tid)
        self.addCleanup(probe.close)
        self.assertTrue(
            probe.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                          (f"oikonome:email:{self.tid}",)).fetchone()["ok"],
            "a guard that never unlocks would silence the cadence forever")

    def test_a_send_that_raises_still_releases_the_lock(self):
        from oikonome.jobs import worker
        with mock.patch.object(worker, "emails_due", return_value=["daily"]), \
             mock.patch.object(worker, "email_tenant",
                               side_effect=RuntimeError("smtp down")):
            with self.assertRaises(RuntimeError):
                worker._email_if_due(self.tid)
        probe = tenancy.tenant_connect(self.tid)
        self.addCleanup(probe.close)
        self.assertTrue(
            probe.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                          (f"oikonome:email:{self.tid}",)).fetchone()["ok"],
            "one failed send must not wedge the cadence permanently")


class ConfigTxnCoverageTests(unittest.TestCase):
    """Every settings write is a locked read-modify-write.

    The recurring shape is: load the settings blob, change one key, write the
    WHOLE document back — which silently discards any concurrent change to a
    different key. These tests pin the property rather than the call sites, so
    a NEW endpoint written the old way fails here instead of shipping.
    """

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_every_settings_write_path_takes_the_lock(self):
        """Structural: no function anywhere may pair load_config with
        save_config. Read-only load_config is fine and deliberately NOT
        locked — see budget.config_txn's docstring on why locking reads
        would be the worse trade.

        It walks the whole package, by AST so that methods and nested
        functions count too, not just top-level `def`s: a property test
        that covers one module certifies one module."""
        import ast
        import pathlib
        import oikonome
        root = pathlib.Path(oikonome.__file__).parent
        # budget.py DEFINES the pair (load_config's seed-persist and
        # config_txn's own body) — the rule is about callers.
        skip = {root / "engine" / "budget.py"}
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if path in skip:
                continue
            tree = ast.parse(path.read_text(), str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef,
                                         ast.AsyncFunctionDef)):
                    continue
                calls = {
                    n.func.attr for n in ast.walk(node)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                }
                # The MIXED shapes count too. Matching only the
                # helper-to-helper pair would leave the same lost update
                # reachable by dropping to raw SQL on either side. No such
                # site exists today; this keeps it that way.
                strs = " ".join(
                    n.value for n in ast.walk(node)
                    if isinstance(n, ast.Constant)
                    and isinstance(n.value, str))
                raw_write = ("INSERT INTO tenant_settings" in strs
                             or "UPDATE tenant_settings" in strs)
                raw_read = "config FROM tenant_settings" in strs
                locked = "config_txn" in calls or "FOR UPDATE" in strs
                pairs = (
                    ({"load_config", "save_config"} <= calls, "helper pair"),
                    ("load_config" in calls and raw_write,
                     "load_config + raw write"),
                    (raw_read and "save_config" in calls,
                     "raw read + save_config"),
                )
                for matched, kind in pairs:
                    if matched and not locked:
                        offenders.append(
                            f"{path.relative_to(root)}:{node.lineno} "
                            f"{node.name} [{kind}]")
                        break
        self.assertEqual(
            [], offenders,
            "these read the settings document and write it back without "
            "budget.config_txn — the lost-update shape: "
            + ", ".join(offenders))

    def test_a_concurrent_write_cannot_clobber_an_earlier_one(self):
        """Behavioural: two writers, interleaved the way two browser tabs
        are. Without the lock the second read sees the pre-first state and
        writes it back, dropping the first change."""
        from oikonome.engine import budget
        conn = make_db()
        self.addCleanup(conn.close)
        tid = str(conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(conn)
        other = tenancy.tenant_connect(tid)
        self.addCleanup(other.close)

        with budget.config_txn(conn) as cfg:
            cfg["first_writer"] = True
            # the second writer arrives mid-flight and must BLOCK on the row
            # lock rather than read the stale document
            import threading
            done = threading.Event()

            def second():
                try:
                    with budget.config_txn(other) as c2:
                        c2["second_writer"] = True
                finally:
                    done.set()
            t = threading.Thread(target=second, daemon=True)
            t.start()
            self.assertFalse(
                done.wait(1.0),
                "the second writer returned while the first still held the "
                "row — the lock is not being taken, which is exactly how an "
                "inert FOR UPDATE looks")
        t.join(timeout=10)

        # close the pooled conn — a leak here shrinks the shared tenant
        # pool for the rest of the suite run
        _c = tenancy.tenant_connect(tid)
        try:
            final = budget.load_config(_c)
        finally:
            _c.close()
        self.assertTrue(final.get("first_writer"),
                        "the first writer's change was silently discarded — "
                        "this is the lost update itself")
        self.assertTrue(final.get("second_writer"))


class RawSettingsWriteTests(unittest.TestCase):
    """The raw-SQL restore write path holds the lost-update property too.

    `ConfigTxnCoverageTests` above pins the lost-update property by the pair
    of helper CALLS — `load_config` + `save_config` in one function. The
    restore path never used those helpers: it reads the settings document,
    merges the ZIP's keys over it and writes the whole thing back in raw
    SQL, so the AST guard walks straight past it for the same reason a
    guard scoped to one module walks past every other one.

    `ON CONFLICT DO UPDATE` does take a row lock, but only at WRITE time —
    after the stale read has already happened — so the merge is computed
    from a document that a concurrent writer has since changed.
    """

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    @staticmethod
    def _zip(config: dict) -> bytes:
        """A minimal export ZIP, the way /export writes one."""
        import csv
        import io
        import json
        import zipfile
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            s = io.StringIO()
            w = csv.DictWriter(s, fieldnames=["config"])
            w.writeheader()
            w.writerow({"config": json.dumps(config)})
            z.writestr("tenant_settings.csv", s.getvalue())
        return buf.getvalue()

    def test_restore_does_not_merge_over_a_stale_settings_read(self):
        """The nightly budget seed commits while a restore is in flight.

        Without the lock the restore's read happens first, the seed commits,
        and then the restore writes back a document computed from the
        pre-seed state — the seed is silently gone.
        """
        import threading
        from oikonome.engine import budget
        from oikonome.sync import restore

        conn = make_db()
        self.addCleanup(conn.close)
        tid = str(conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])
        write_config(conn)

        other = tenancy.tenant_connect(tid)
        self.addCleanup(other.close)

        data = self._zip({"restored_key": "from-zip"})
        failed: list[BaseException] = []
        finished = threading.Event()

        def do_restore():
            try:
                restore.restore_zip(conn, data)
            except BaseException as e:      # noqa: BLE001 — reported below
                failed.append(e)
            finally:
                finished.set()

        t = threading.Thread(target=do_restore, daemon=True)
        with budget.config_txn(other) as cfg:
            cfg["seeded_by_worker"] = True
            # the restore arrives mid-flight, exactly as the hourly restore
            # and the nightly seed can overlap on a real instance
            t.start()
            self.assertFalse(
                finished.wait(1.5),
                "the restore wrote the settings row while another writer "
                "still held it — it is not taking the lock at all")
        t.join(timeout=20)
        self.assertFalse(finished.is_set() and failed,
                         f"restore raised: {failed[0] if failed else None}")
        self.assertFalse(t.is_alive(), "restore never returned")

        _c = tenancy.tenant_connect(tid)   # returned below (pool leak otherwise)
        try:
            final = budget.load_config(_c)
        finally:
            _c.close()
        self.assertTrue(
            final.get("seeded_by_worker"),
            "the concurrent writer's key was merged out of existence — the "
            "restore computed its merge from a document it read BEFORE that "
            "writer committed, which is the lost update in raw SQL")
        self.assertEqual(final.get("restored_key"), "from-zip",
                         "the ZIP's own keys must still land")

    def test_no_raw_read_modify_write_of_the_settings_row_skips_the_lock(self):
        """Structural twin of the AST guard, for the raw-SQL shape.

        Any function that both reads `tenant_settings` and writes it back is
        a read-modify-write of the whole document and must take the row lock
        in the READ, not rely on the write's own lock.
        """
        import ast
        import pathlib

        import oikonome
        root = pathlib.Path(oikonome.__file__).parent
        # budget.py owns the locking helper itself
        skip = {root / "engine" / "budget.py"}
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if path in skip:
                continue
            src = path.read_text()
            if "tenant_settings" not in src:
                continue
            tree = ast.parse(src, str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef,
                                         ast.AsyncFunctionDef)):
                    continue
                seg = (ast.get_source_segment(src, node) or "").lower()
                reads = "from tenant_settings" in seg
                writes = ("into tenant_settings" in seg
                          or "update tenant_settings" in seg)
                if reads and writes and "for update" not in seg:
                    offenders.append(
                        f"{path.relative_to(root)}:{node.lineno} {node.name}")
        self.assertEqual(
            [], offenders,
            "these read the settings document and write it back without "
            "locking it first — ON CONFLICT DO UPDATE locks too late to "
            "make the merge atomic: " + ", ".join(offenders))
