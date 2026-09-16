"""A restore archive is streamed to disk, never held in the handler.

Restoring an export is how somebody moves a household to a new machine, and
a household's whole archive — every transaction, every receipt image — is
bigger than any statement file. The import door reads an upload into memory
(a chunk list, then a joined copy), so its ceiling could not simply be
raised: a large archive would OOM a shared container before the restore
started. Hence a separate door that writes the bytes straight to a temp
file and hands on a mapping of it.

What these pin: the handler never materializes the body, the ceiling is
enforced WHILE streaming (an oversized upload is abandoned, not read to the
end), and what comes back is still a usable ZIP.
"""

import asyncio
import io
import unittest
import zipfile

from oikonome.web import pages

from .util import _ensure_db, make_db, write_config

MB = 1024 * 1024


class _ChunkedUpload:
    """An UploadFile stand-in that serves `total` bytes and records every
    read size it was asked for — the evidence that nothing read the body
    whole."""

    def __init__(self, total: int, head: bytes = b""):
        self.total = total
        self.head = head
        self.sent = 0
        self.reads: list[int] = []

    async def read(self, size: int = -1) -> bytes:
        self.reads.append(size)
        if size is None or size < 0:            # a whole-body read
            size = self.total - self.sent
        take = min(size, self.total - self.sent)
        if take <= 0:
            return b""
        start, self.sent = self.sent, self.sent + take
        out = bytearray(take)
        # the first bytes are real content; the rest is filler
        for i in range(start, min(start + take, len(self.head))):
            out[i - start] = self.head[i]
        return bytes(out)


def _zip_bytes() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("tenant_settings.csv", "id,config\n")
    return buf.getvalue()


class RestoreUploadStreamingTests(unittest.TestCase):

    def test_a_sixty_megabyte_archive_is_accepted_without_a_whole_body_read(
            self):
        """60 MB is over the 50 MB import ceiling and well under the restore
        one. It arrives a megabyte at a time and is never asked for whole:
        a single unbounded read() is the OOM this door exists to avoid."""
        up = _ChunkedUpload(60 * MB)
        view = asyncio.run(pages.read_restore_capped(up))
        self.addCleanup(view.close)
        self.assertIsNotNone(view, "a 60 MB archive was refused")
        self.assertEqual(len(view), 60 * MB)
        self.assertNotIsInstance(view, bytes,
                                 "the archive was copied into memory")
        self.assertTrue(up.reads, "the upload was never read")
        self.assertTrue(all(0 < n <= MB for n in up.reads),
                        f"the handler asked for the body whole: {up.reads}")

    def test_what_comes_back_is_still_a_readable_archive(self):
        """The mapping is what the restore path opens, so it has to behave
        like the bytes did — zipfile takes it either way."""
        payload = _zip_bytes()
        up = _ChunkedUpload(len(payload), head=payload)
        view = asyncio.run(pages.read_restore_capped(up))
        self.addCleanup(view.close)
        with zipfile.ZipFile(io.BytesIO(view)) as z:    # bytes-like…
            self.assertIn("tenant_settings.csv", z.namelist())
        view.seek(0)
        with zipfile.ZipFile(view) as z:                # …and file-like
            self.assertIn("tenant_settings.csv", z.namelist())

    def test_an_oversized_archive_is_abandoned_mid_stream(self):
        """The ceiling is enforced while the bytes arrive. Reading to the
        end first would mean spooling the whole hostile upload to disk to
        find out we did not want it."""
        up = _ChunkedUpload(20 * MB)
        view = asyncio.run(pages.read_restore_capped(up, cap=4 * MB))
        self.assertIsNone(view, "an over-cap archive was accepted")
        self.assertLessEqual(sum(n for n in up.reads if n > 0), 8 * MB,
                             "the whole oversized upload was read anyway")

    def test_an_empty_upload_comes_back_empty_rather_than_raising(self):
        """A zero-byte file cannot be mapped; the restore path refuses it
        as a bad archive, which is a message, not a 500."""
        view = asyncio.run(pages.read_restore_capped(_ChunkedUpload(0)))
        self.assertEqual(view, b"")

    def test_the_restore_ceiling_is_the_documented_one(self):
        self.assertEqual(pages.RESTORE_MAX_UPLOAD, 500 * MB)
        self.assertGreater(pages.RESTORE_MAX_UPLOAD, pages.MAX_UPLOAD)


class RestoreUploadRouteTests(unittest.TestCase):
    """The import door routes a ZIP through the streaming read, and what it
    hands the restore job is the mapping — not a copy of the archive."""

    @classmethod
    def setUpClass(cls):
        _ensure_db()

    def test_a_zip_reaches_the_restore_job_as_a_mapping(self):
        from oikonome.sync import restore_job
        conn = make_db()
        self.addCleanup(conn.close)
        write_config(conn)
        tid = str(conn.execute(
            "SELECT current_setting('app.tenant_id') AS t").fetchone()["t"])

        payload = _zip_bytes()
        got = {}

        def fake_start(tenant_id, data):
            got["data"] = data
            # the callee's own handling has to work on it unchanged
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                got["names"] = z.namelist()
            return {"restore_started": True}

        real = restore_job.start
        restore_job.start = fake_start
        try:
            up = _ChunkedUpload(len(payload), head=payload)
            up.filename = "oikonome-export.zip"
            asyncio.run(pages.import_submit(
                user={"tenant_id": tid, "role": "owner"},
                account_id="", amount_sign="bank", file=up))
        finally:
            restore_job.start = real
        self.assertEqual(got["names"], ["tenant_settings.csv"])
        self.assertNotIsInstance(got["data"], bytes,
                                 "the handler copied the archive into memory")
        self.assertTrue(all(0 < n <= MB for n in up.reads),
                        f"the handler asked for the body whole: {up.reads}")


if __name__ == "__main__":
    unittest.main()
