"""An uploaded ZIP is bounded before anything parses it.

`zipfile.ZipFile()` builds an object for every central-directory record
before a caller can look at the archive. A small hand-made file can list a
million members that all point at one empty entry, and opening it costs
hundreds of megabytes in the web process every household shares — before
the role check, the step-up or any size rule. So the member count and the
directory size are read from the end-of-central-directory record first, at
one choke point (sync/upload_zip.py), and every door that reads uploaded
ZIP bytes goes through it.

Here: the hand-made archive is refused in bounded time and bounded memory,
at each door, before the step-up is asked; zip64 and plain-ended exports
still open; and no new reader of uploaded ZIP bytes can appear beside the
choke point without a test failing."""

import ast
import io
import json
import os
import pathlib
import struct
import subprocess
import sys
import unittest
import uuid
import zipfile
from unittest import mock

from oikonome.sync import restore, upload_zip

SERVER = pathlib.Path(__file__).resolve().parents[1]
PACKAGE = SERVER / "oikonome"
PW = "correct-horse-battery"


def _bomb(records: int, *, listed: int | None = None,
          zip64: bool = False) -> bytes:
    """`records` central-directory entries, each 47 bytes and all naming
    the one empty local entry at offset 0. `listed` is the member count the
    end record claims (the true count by default)."""
    name = b"a"
    local = struct.pack("<4s5H3L2H", b"PK\x03\x04", 20, 0, 0, 0, 0,
                        0, 0, 0, len(name), 0) + name
    rec = struct.pack("<4s6H3L5H2L", b"PK\x01\x02", 20, 20, 0, 0, 0, 0,
                      0, 0, 0, len(name), 0, 0, 0, 0, 0, 0) + name
    cd = rec * records
    listed = records if listed is None else listed
    cd_at, cd_size = len(local), len(cd)
    if not zip64:
        eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0,
                           listed & 0xFFFF, listed & 0xFFFF,
                           cd_size, cd_at, 0)
        return local + cd + eocd
    rec64_at = cd_at + cd_size
    rec64 = struct.pack("<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0,
                        listed, listed, cd_size, cd_at)
    loc = struct.pack("<4sLQL", b"PK\x06\x07", 0, rec64_at, 1)
    eocd = struct.pack("<4s4H2LH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF,
                       0xFFFFFFFF, 0xFFFFFFFF, 0)
    return local + cd + rec64 + loc + eocd


def _small_export() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("tenant_settings.csv", "config\n{}\n")
        z.writestr("accounts.csv", "id,name\n")
    return buf.getvalue()


class TheEndRecordIsReadBeforeTheArchiveIsOpened(unittest.TestCase):
    def test_a_million_listed_members_are_refused(self):
        for variant in (dict(), dict(zip64=True), dict(listed=1)):
            with self.subTest(**variant):
                problem = upload_zip.central_directory_problem(
                    _bomb(1_000_000, **variant))
                self.assertIsNotNone(problem)

    def test_one_member_past_the_cap_is_refused_and_the_cap_itself_opens(self):
        cap = upload_zip.MAX_ARCHIVE_MEMBERS
        self.assertIn("lists", upload_zip.central_directory_problem(
            _bomb(cap + 1)))
        self.assertIsNone(upload_zip.central_directory_problem(_bomb(cap)))
        self.assertEqual(len(upload_zip.open_uploaded(
            _bomb(cap)).infolist()), cap)

    def test_a_directory_understating_its_count_is_refused_by_its_size(self):
        n = upload_zip.MAX_CENTRAL_DIRECTORY_BYTES // 47 + 1
        self.assertIn("file list", upload_zip.central_directory_problem(
            _bomb(n, listed=3)))

    def test_what_the_reader_cannot_settle_is_refused(self):
        good = _bomb(3)
        cases = {
            "no end record": good[:-22],
            "empty": b"",
            "not a zip": b"Date,Amount\n2026-01-02,4.00\n",
            "two disks": good[:-18] + struct.pack("<H", 1) + good[-16:],
            # the directory claims more bytes than precede the end record
            "directory past the file": (
                good[:-10] + struct.pack("<L", 10_000) + good[-6:]),
            # a declared offset that does not fit before the directory
            "offset past the directory": (
                good[:-6] + struct.pack("<L", 999) + good[-2:]),
        }
        for why, data in cases.items():
            with self.subTest(why):
                self.assertEqual(upload_zip.central_directory_problem(data),
                                 upload_zip.UNREADABLE)
        z64 = _bomb(3, zip64=True)
        loc_at = len(z64) - 22 - 20
        moved = (z64[:loc_at + 8] + struct.pack("<Q", 7)
                 + z64[loc_at + 16:])
        self.assertEqual(upload_zip.central_directory_problem(moved),
                         upload_zip.UNREADABLE)

    def test_it_reads_the_record_zipfile_reads(self):
        """The bound is only a bound if it is the directory zipfile then
        reads: a comment, a zip64 end and a plain end all agree."""
        plain = _small_export()
        with_comment = io.BytesIO()
        with zipfile.ZipFile(with_comment, "w") as z:
            z.writestr("accounts.csv", "id\n")
            z.comment = b"a note the writer added " * 20
        with mock.patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 1):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for i in range(4):
                    z.writestr(f"t{i}.csv", "id\n")
            forced64 = buf.getvalue()
        self.assertIn(b"PK\x06\x07", forced64)
        for data in (plain, with_comment.getvalue(), forced64,
                     _bomb(4, zip64=True)):
            with self.subTest(size=len(data)):
                self.assertIsNone(upload_zip.central_directory_problem(data))
                rec = zipfile._EndRecData(io.BytesIO(data))
                z = upload_zip.open_uploaded(data)
                self.assertEqual(rec[zipfile._ECD_ENTRIES_TOTAL],
                                 len(z.infolist()))

    def test_every_form_an_upload_arrives_in_is_read_in_place(self):
        import mmap
        import tempfile
        data = _small_export()
        with tempfile.TemporaryFile() as f:
            f.write(data)
            f.flush()
            m = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            for form in (data, bytearray(data), memoryview(data), m,
                         io.BytesIO(data)):
                with self.subTest(type(form).__name__):
                    z = upload_zip.open_uploaded(form)
                    self.assertIn("accounts.csv", z.namelist())
            m.close()


_PROBE = r"""
import json, resource, sys, time
sys.path.insert(0, sys.argv[1])
from oikonome.sync import restore
from tests.test_an_uploaded_zip_is_bounded_before_it_is_opened import _bomb
if sys.argv[2] == "unguarded":
    from oikonome.sync import upload_zip
    upload_zip.central_directory_problem = lambda d: None
    # the door as it was: the archive opened, then judged
    restore._upload_zip.central_directory_problem = lambda d: None
bomb = _bomb(1_000_000)
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
t = time.monotonic()
refusal = restore.foreign_archive_refusal(bomb)
try:
    restore._restore_zip(None, bomb, None)
    zipped = None
except ValueError as e:
    zipped = str(e)
except Exception as e:
    zipped = None
took = time.monotonic() - t
grew = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before
print(json.dumps({"refusal": refusal, "zipped": zipped, "seconds": took,
                  "grew_kb": grew}))
"""


def _probe(mode: str) -> dict:
    out = subprocess.run([sys.executable, "-c", _PROBE, str(SERVER), mode],
                         cwd=str(SERVER), capture_output=True, text=True,
                         timeout=120, env=dict(os.environ))
    if out.returncode != 0:
        raise AssertionError(out.stderr[-2000:])
    return json.loads(out.stdout.strip().splitlines()[-1])


class TheRefusalCostsNeitherTimeNorMemory(unittest.TestCase):
    def test_a_million_member_archive_costs_the_process_almost_nothing(self):
        got = _probe("guarded")
        self.assertIn("lists", got["refusal"] or "")
        self.assertIn("lists", got["zipped"] or "")
        self.assertLess(got["seconds"], 0.5, got)
        # the archive is 47 MB already held; opening it cost ~345 MB more
        self.assertLess(got["grew_kb"], 8 * 1024, got)


def _signed_up(case):
    from fastapi.testclient import TestClient

    from oikonome.db import tenancy
    from oikonome.web import security
    import oikonome.web.app as appmod
    os.environ["OIKONOME_DEV"] = "1"
    appmod.DEV_MODE = True
    security._limiter._hits.clear()
    c = TestClient(appmod.app)
    email = f"zb-{uuid.uuid4().hex[:10]}@example.dev"
    r = c.post("/api/signup", data={"email": email, "password": PW})
    case.assertEqual(r.status_code, 200, r.text)
    admin = tenancy.admin_connect()
    try:
        # no step-up in force: a door asking for one answers 403
        admin.execute("UPDATE sessions SET elevated_at = NULL WHERE "
                      "user_id = (SELECT id FROM users WHERE email=%s)",
                      (email,))
    finally:
        admin.close()
    return c


class EveryDoorRefusesItBeforeTheStepUp(unittest.TestCase):
    """The doors that read an uploaded ZIP: the API import (its restore
    branch), the folder plan, and the restore job every door starts."""

    @classmethod
    def setUpClass(cls):
        from .util import _ensure_db
        _ensure_db()

    def setUp(self):
        self.bomb = _bomb(upload_zip.MAX_ARCHIVE_MEMBERS * 20)
        self.c = _signed_up(self)

    def test_the_api_import_door(self):
        with mock.patch.object(zipfile.ZipFile, "_RealGetContents",
                               side_effect=AssertionError("opened")):
            r = self.c.post("/api/import", files={
                "file": ("export.zip", self.bomb, "application/zip")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("lists", r.json()["result"]["error"])

    def test_the_folder_plan(self):
        with mock.patch.object(zipfile.ZipFile, "_RealGetContents",
                               side_effect=AssertionError("opened")):
            r = self.c.post("/api/import/bulk/analyze", files=[
                ("files", ("export.zip", self.bomb, "application/zip"))])
        self.assertEqual(r.status_code, 200, r.text)
        entry = r.json()["files"][0]
        self.assertEqual(entry["action"], "skip")
        self.assertIn("lists", entry["note"])

    def test_the_restore_job(self):
        from oikonome.db import tenancy
        from .test_account_erasure_export_and_consent import _seed_tenant
        tid, _ = _seed_tenant(with_data=False)
        conn = tenancy.tenant_connect(tid)
        try:
            with mock.patch.object(zipfile.ZipFile, "_RealGetContents",
                                   side_effect=AssertionError("opened")), \
                    self.assertRaises(ValueError) as e:
                restore.restore_zip(conn, self.bomb)
        finally:
            conn.close()
        self.assertIn("lists", str(e.exception))

    def test_a_household_export_still_restores(self):
        from oikonome.db import tenancy
        from oikonome.sync import export
        from .test_account_erasure_export_and_consent import _seed_tenant
        tid, _ = _seed_tenant()
        conn = tenancy.tenant_connect(tid)
        try:
            data = export.build_zip(conn)
        finally:
            conn.close()
        self.assertIsNone(upload_zip.central_directory_problem(data))
        self.assertIsNone(restore.foreign_archive_refusal(data))
        other, _ = _seed_tenant(with_data=False)
        conn = tenancy.tenant_connect(other)
        try:
            counts = restore.restore_zip(conn, data)
        finally:
            conn.close()
        self.assertGreater(counts.get("accounts", 0), 0, counts)


# Opening a ZIP for writing is the app's own output; opening one for
# reading is reading something handed in, and that goes through the
# choke point. Other decompressors have no upload door today: one that
# gains a use here has to show it is bounded the same way.
_WRITE_MODES = {"w", "a", "x"}
_DECOMPRESSORS = {"tarfile", "gzip", "lzma", "bz2"}


def _zipfile_reads(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = (f.attr if isinstance(f, ast.Attribute)
                else f.id if isinstance(f, ast.Name) else None)
        if name in ("is_zipfile", "Path") and isinstance(
                f, ast.Attribute) and getattr(f.value, "id", "") in (
                    "zipfile", "_z"):
            yield node.lineno
        if name != "ZipFile":
            continue
        mode = (node.args[1] if len(node.args) > 1 else next(
            (k.value for k in node.keywords if k.arg == "mode"), None))
        if not (isinstance(mode, ast.Constant)
                and mode.value in _WRITE_MODES):
            yield node.lineno


class NoReaderOfUploadedZipBytesBesideTheChokePoint(unittest.TestCase):
    def test_zipfile_opens_for_reading_only_in_upload_zip(self):
        found = []
        for path in sorted(PACKAGE.rglob("*.py")):
            rel = path.relative_to(SERVER).as_posix()
            if rel == "oikonome/sync/upload_zip.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            found += [f"{rel}:{n}" for n in _zipfile_reads(tree)]
        self.assertEqual(found, [], "open uploaded ZIP bytes through "
                         "sync.upload_zip.open_uploaded, which bounds the "
                         "central directory first")

    def test_no_other_decompressor_is_imported(self):
        found = []
        for path in sorted(PACKAGE.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = ([a.name for a in node.names]
                         if isinstance(node, ast.Import)
                         else [node.module or ""]
                         if isinstance(node, ast.ImportFrom)
                         and node.level == 0 else [])
                found += [f"{path.relative_to(SERVER)}:{node.lineno}"
                          for n in names
                          if n.split(".")[0] in _DECOMPRESSORS]
        self.assertEqual(found, [])

    def test_the_walk_sees_a_reader_it_must_refuse(self):
        tree = ast.parse("import zipfile\nzipfile.ZipFile(io.BytesIO(b))\n"
                         "zipfile.ZipFile(buf, 'w')\n"
                         "zipfile.ZipFile(p, mode='r')\n")
        self.assertEqual(list(_zipfile_reads(tree)), [2, 4])


if __name__ == "__main__":
    unittest.main()
