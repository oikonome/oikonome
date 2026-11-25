"""What a hostile archive lands, every page still reads, and the household
still exports and restores (specs/invariants-and-fuzzing.md, R3).

The cell shapes of tests/test_a_hostile_archive_cell_never_breaks_the_restore.py
that land a value (not the non-finite numbers or the bytes that are not
UTF-8, which refuse the archive) are spread over
one real export (tests/roundtrip_builders.py): in every member but the
settings document, row i takes shape i + offset in column i + offset, so
each archive carries every shape in every member at once. The archive is restored into a signed-up household
through the restore job's own path (`restore_job.restore_now`), and then:

* every GET door under /api with no path parameter answers below 500
  (one 503 is a door's stated need, tests/body_fuzz.ALLOWED_503), in the
  shape it documents (tests/api_conformance.py);
* the daily email's context builds and renders;
* the household exports, and that export restores into an empty
  household without stopping on an unhandled exception.

Two archives by default (offsets 0 and 1); OIKONOME_PROPERTY=long runs
an offset per shape, each over the rows in their stable order and again
reversed.
"""

from __future__ import annotations

import time
import unittest
from unittest import mock

from fastapi.routing import APIRoute

from oikonome.db import tenancy
from oikonome.sync import export, restore, restore_job

from . import body_fuzz, props
from . import roundtrip_builders as rb
from .api_conformance import _flat
from .money_surfaces import at_today
from .restore_invariants import fresh_household, rewrite_members
from .test_a_hostile_archive_cell_never_breaks_the_restore import (
    CELLS, _h, _members)
from .util import TODAY

# the shapes that refuse the archive whole: nothing of them lands
REFUSED = {"nan", "infinity", "int-past-float", "int-past-parse",
           "exponent", "lone-surrogate"}
LANDING = tuple((n, make) for n, make in CELLS if n not in REFUSED)


class _Now:
    """The post-restore categorize pass runs in a thread; here it runs
    before the pages are read, so they read what it wrote."""

    def __init__(self, target=None, **_kw):
        self.target = target

    def start(self):
        self.target()


def _mixed(member: str, offset: int, reverse: bool = False):
    """Row i of the member, in a STABLE order (its cells, as text), takes
    shape i + offset: the export reads each table in the database's heap
    order, so without the sort which row met which shape — and so what
    the property covered — changed from one database to the next.
    `reverse` deals the shapes over the same rows the other way round."""
    def edit(recs):
        if len(recs) < 2:
            return recs
        width = len(recs[0])
        start = _h(member) % width
        out = [recs[0]]
        body = sorted(recs[1:], key=lambda r: [str(c) for c in r],
                      reverse=reverse)
        for i, row in enumerate(body):
            row = list(row)
            c = (start + i + offset) % width
            _name, make = LANDING[(i + offset) % len(LANDING)]
            if c < len(row):
                row[c] = make(row[c])
            out.append(row)
        return out
    return edit


def _pages(app) -> list[str]:
    return sorted({r.path for r in _flat(app.routes)
                   if isinstance(r, APIRoute) and "GET" in r.methods
                   and r.path.startswith("/api/") and "{" not in r.path
                   and r.endpoint.__module__.startswith("oikonome.")
                   and not body_fuzz.RISKY.search(r.path)})


class AHostileRestoreStillReads(unittest.TestCase):
    maxDiff = None

    @classmethod
    def setUpClass(cls):
        cls.started = time.monotonic()
        src = fresh_household()
        try:
            rb.build(src, 1)
            cls.data = export.build_zip(src)
        finally:
            src.close()
        cls.members = _members(cls.data)

    @classmethod
    def tearDownClass(cls):
        print(f"\n[timing] {cls.__name__}: "
              f"{time.monotonic() - cls.started:.1f}s", flush=True)

    def test_every_page_reads_after_a_hostile_restore(self):
        import oikonome.web.app as appmod
        from oikonome.web import report, security
        appmod.DEV_MODE = True
        pages = _pages(appmod.app)
        self.assertGreater(len(pages), 40, pages)
        runs = ([(o, rev) for rev in (False, True)
                 for o in range(len(LANDING))] if props.LONG
                else [(0, False), (1, False)])
        for offset, reverse in runs:
            # the settings document is one cell: a shape it refuses (one
            # nested past the walk) refuses the archive, so it keeps its
            # own; the hostile-cell property holds that refusal
            data = rewrite_members(self.data, {
                m: _mixed(m, offset, reverse) for m in self.members
                if m != "tenant_settings.csv"})
            client, _e, tid = body_fuzz.signup(appmod.app, "r3read")
            conn = tenancy.tenant_connect(tid)
            try:
                with self.subTest(offset=offset, reverse=reverse), at_today():
                    with mock.patch("threading.Thread", _Now):
                        restore_job.restore_now(conn, data, tenant_id=tid)
                    broken = []
                    for path in pages:
                        security._limiter._hits.clear()
                        try:
                            r = client.get(path)
                        except AssertionError as e:
                            # the reply is not the shape its door
                            # documents (tests/api_conformance.py)
                            broken.append("\n".join(
                                ln for ln in str(e).splitlines()
                                if "errors.pydantic" not in ln)[:1200])
                            continue
                        if body_fuzz.server_error(r):
                            broken.append(f"{path} -> {r.status_code} "
                                          f"{r.text[:120]}")
                    if broken:
                        self.fail(f"offset {offset}"
                                  + (" reversed" if reverse else "")
                                  + ":\n" + "\n".join(broken))
                    report.build(report.gather(conn, TODAY))
                    again = export.build_zip(conn)
                    other = fresh_household()
                    try:
                        restore.restore_outcomes(other, again)
                    finally:
                        other.close()
            finally:
                conn.close()
                client.close()


if __name__ == "__main__":
    unittest.main()
