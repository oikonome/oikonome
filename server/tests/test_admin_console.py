"""The admin console.

The console is the OPERATOR's surface: enabled only by
OIKONOME_ADMIN_TOKEN, its own session cookie (never a tenant session),
optional IP allowlist that cloaks with 404, all actions audit-logged,
and everything running on the admin connection (the app role holds no
grants on admin_sessions/admin_audit — migration 026).
"""

import os
import pathlib
import unittest
import unittest.mock
import uuid

from fastapi.testclient import TestClient

from oikonome.db import tenancy

from .util import _ensure_db, make_db

TOKEN = "test-admin-token-" + "x" * 32


def _admin_rows(sql, params=()):
    admin = tenancy.admin_connect()
    try:
        return admin.execute(sql, params).fetchall()
    finally:
        admin.close()


class _Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        cls.appmod = appmod

    def setUp(self):
        os.environ["OIKONOME_ADMIN_TOKEN"] = TOKEN
        from oikonome.web import security
        security._limiter._hits.clear()
        self.client = TestClient(self.appmod.app)

    def tearDown(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)
        os.environ.pop("OIKONOME_ADMIN_IPS", None)

    def _login(self):
        r = self.client.post("/admin/console/login", data={"token": TOKEN},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return r


class GateTests(_Base):
    def test_console_off_is_404_everywhere(self):
        os.environ.pop("OIKONOME_ADMIN_TOKEN", None)
        for method, path in (("get", "/admin/console"),
                             ("post", "/admin/console/login"),
                             ("post", "/admin/console/invite")):
            r = getattr(self.client, method)(path, follow_redirects=False)
            self.assertEqual(r.status_code, 404, path)

    def test_ip_allowlist_cloaks_with_404(self):
        # the TestClient's client address is not a valid IP, so any
        # allowlist excludes it — the console must vanish, not 403
        os.environ["OIKONOME_ADMIN_IPS"] = "203.0.113.0/24"
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 404)

    def test_wrong_token_no_cookie(self):
        r = self.client.post("/admin/console/login",
                             data={"token": "nope"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("not right", r.text)
        self.assertNotIn("oikonome_admin", r.cookies)

    def test_unauthed_get_renders_login_not_dashboard(self):
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Operator token", r.text)
        self.assertNotIn("Audit trail", r.text)

    def test_tenant_session_is_not_an_admin_session(self):
        # a signed-in tenant cookie lives under a different name entirely;
        # even copying its value into the admin cookie must not authenticate
        self.client.cookies.set("oikonome_admin", "some-tenant-token")
        r = self.client.get("/admin/console")
        self.assertIn("Operator token", r.text)

    def test_actions_bounce_without_session(self):
        r = self.client.post("/admin/console/invite",
                             data={"email": "x@y.dev"},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/admin/console")


class SessionTests(_Base):
    def test_login_dashboard_logout(self):
        self._login()
        r = self.client.get("/admin/console")
        self.assertIn("Audit trail", r.text)
        self.assertIn("Fleet", r.text)
        # server-health section renders (users online, DB, host, workers)
        self.assertIn("System health", r.text)
        self.assertIn("users online", r.text)
        self.assertIn("PostgreSQL", r.text)
        # login is audit-logged
        self.assertTrue(_admin_rows(
            "SELECT 1 FROM admin_audit WHERE action='login'"))
        r = self.client.post("/admin/console/logout",
                             follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        r = self.client.get("/admin/console")
        self.assertIn("Operator token", r.text)


class ActionTests(_Base):
    def test_invite_mints_shows_link_and_audits(self):
        self._login()
        email = f"console-{uuid.uuid4().hex[:8]}@x.dev"
        r = self.client.post("/admin/console/invite",
                             data={"email": email, "note": "first invite"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("/signup?invite=", r.text)
        self.assertTrue(_admin_rows(
            "SELECT 1 FROM signup_invites WHERE email=%s AND used_at IS "
            "NULL", (email,)))
        self.assertTrue(_admin_rows(
            "SELECT 1 FROM admin_audit WHERE action='invite' AND "
            "target=%s", (email,)))

    def test_the_invite_lifetime_is_bounded_like_its_free_period(self):
        """`days` becomes timedelta(days=days). Zero or negative mints a link
        that is already expired — an operator sends an invite nobody can
        claim and nothing says so — and a value past timedelta's ceiling
        raises OverflowError, which leaves the console as a 500. Every raw
        form value that reaches arithmetic is clamped."""
        self._login()
        for days in (0, -30, 10 ** 12):
            email = f"ttl-{uuid.uuid4().hex[:8]}@x.dev"
            r = self.client.post("/admin/console/invite",
                                 data={"email": email, "days": str(days)})
            self.assertEqual(r.status_code, 200, f"days={days}: {r.text}")
            self.assertIn("/signup?invite=", r.text)
            row = _admin_rows(
                "SELECT expires_at > now() AS live, "
                "       expires_at < now() + interval '3651 days' AS capped "
                "FROM signup_invites WHERE email=%s", (email,))[0]
            self.assertTrue(row["live"],
                            f"days={days} minted an already-dead invite")
            self.assertTrue(row["capped"],
                            f"days={days} minted an unbounded invite")

    def test_a_sane_invite_lifetime_is_left_alone(self):
        self._login()
        email = f"ttl-ok-{uuid.uuid4().hex[:8]}@x.dev"
        self.client.post("/admin/console/invite",
                         data={"email": email, "days": "7"})
        row = _admin_rows(
            "SELECT expires_at BETWEEN now() + interval '6 days' "
            "  AND now() + interval '8 days' AS ok "
            "FROM signup_invites WHERE email=%s", (email,))[0]
        self.assertTrue(row["ok"])


    def test_resend_verification_mints_token_and_audits(self):
        self._login()
        email = f"unv-{uuid.uuid4().hex[:8]}@x.dev"
        admin = tenancy.admin_connect()
        try:
            tid = tenancy.create_tenant(admin, email)
            uid = admin.execute(
                "INSERT INTO users (tenant_id, email, password_hash) "
                "VALUES (%s, %s, 'x') RETURNING id",
                (tid, email)).fetchone()["id"]
        finally:
            admin.close()
        r = self.client.post("/admin/console/resend-verification",
                             data={"user_id": str(uid)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("re-sent", r.text)
        self.assertTrue(_admin_rows(
            "SELECT 1 FROM email_verifications WHERE user_id=%s", (uid,)))
        self.assertTrue(_admin_rows(
            "SELECT 1 FROM admin_audit WHERE "
            "action='resend_verification' AND target=%s", (email,)))


class PrivilegeTests(_Base):
    def test_app_role_has_no_grants_on_console_tables(self):
        # migration 026 + the migrate.py re-grant block: SQLi through the
        # tenant app must not be able to forge an operator session or
        # scrub the audit trail
        import psycopg
        with psycopg.connect(tenancy.APP_DSN, autocommit=True) as conn:
            for table in ("admin_sessions", "admin_audit"):
                for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                    ok = conn.execute(
                        "SELECT has_table_privilege('oikonome_app', %s, "
                        "%s)", (table, priv)).fetchone()[0]
                    self.assertFalse(ok, f"{table} {priv}")

    def test_app_role_has_no_grants_on_recipient_invites(self):
        """The consent table, in BOTH directions.

        An `accepted_at` row is what makes a household's balances flow to an
        address, so an injected app-role INSERT would be an exfiltration
        channel needing no session and no password. The whole threat model
        rests on the REVOKE, so it is asserted here — the same guard this
        file keeps over the console tables."""
        import psycopg
        with psycopg.connect(tenancy.APP_DSN, autocommit=True) as conn:
            for priv in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                ok = conn.execute(
                    "SELECT has_table_privilege('oikonome_app', "
                    "'recipient_invites', %s)", (priv,)).fetchone()[0]
                self.assertFalse(ok, f"recipient_invites {priv}")


def _vkey(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in version[1:].split("."))


def _manifest_versions() -> list[str]:
    """Every version in the baked manifest, as the file lists them. The
    manifest's shape is the build's — a development tree carries hundreds
    of rows, a published tree one per release — so the tests take their
    known version from the file rather than naming one."""
    import pathlib

    from oikonome.web import adminconsole
    path = pathlib.Path(adminconsole.__file__).resolve().parent.parent / "version_history.tsv"
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            continue
        out.append(line.split("\t")[0])
    return out


class VersionHistoryTests(unittest.TestCase):
    """The console's build-history panel.

    Guards the two things that make it honest: it reads the baked manifest
    (no .git in the image), and it stays SEPARATE from `deployments` — a
    deploy row means "actually deployed here", build history means "this
    version existed". Merging them would invent deploy events.
    """

    def test_manifest_parses_and_is_newest_first(self):
        from oikonome.web import adminconsole
        rows = adminconsole._version_history()
        self.assertTrue(rows, "version_history.tsv missing from the package "
                              "— run scripts/gen-version-history.sh")
        for r in rows:
            self.assertRegex(r["version"], r"^v\d+\.\d+\.\d+$")
            self.assertRegex(r["date"], r"^\d{4}-\d{2}-\d{2}$")
            self.assertTrue(r["sha"] and r["subject"])
        keys = [_vkey(r["version"]) for r in rows]
        self.assertEqual(keys, sorted(keys, reverse=True))

    def test_history_is_the_whole_manifest(self):
        # the whole point of the panel: every version the manifest knows,
        # not the handful of rows `deployments` has carried since it existed
        from oikonome.web import adminconsole
        versions = {r["version"] for r in adminconsole._version_history()}
        self.assertEqual(versions, set(_manifest_versions()))

    def test_missing_manifest_degrades_to_empty(self):
        from unittest import mock

        from oikonome.web import adminconsole
        with mock.patch("pathlib.Path.open", side_effect=OSError):
            self.assertEqual(adminconsole._version_history(), [])


class DeploySummarySourceTests(unittest.TestCase):
    """The deploy panel's per-version 'what changed' must come from the real
    per-version commit subject (build manifest), not the hand-set
    OIKONOME_VERSION_NOTE, which goes stale across deploys: every deploy
    re-stamps the same string, so a whole run of versions reads as one
    unrelated change."""

    def test_manifest_subject_returns_real_per_version_note(self):
        # a known manifest row: the manifest reader never consults the env
        # note, so a stale note set for the same version leaves it unchanged
        from unittest import mock

        from oikonome.db import migrate
        known = _manifest_versions()[0]
        s = migrate._manifest_subject(known)
        self.assertTrue(s)
        with mock.patch.dict(os.environ, {
                "OIKONOME_VERSION_NOTE": "Stale hand-set deploy note",
                "OIKONOME_VERSION_NOTE_FOR": known}):
            self.assertEqual(migrate._manifest_subject(known), s)
            self.assertNotEqual(s, "Stale hand-set deploy note")

    def test_manifest_subject_unknown_version_is_none(self):
        from oikonome.db import migrate
        self.assertIsNone(migrate._manifest_subject("v9.9.9"))

    def test_summary_prefers_manifest_over_env_note(self):
        # a stale env note loses to the manifest
        from unittest import mock

        from oikonome.db import migrate
        known = _manifest_versions()[0]
        with mock.patch.dict(os.environ, {
                "OIKONOME_VERSION_NOTE": "Stale hand-set deploy note",
                "OIKONOME_VERSION_NOTE_FOR": known}):
            self.assertNotEqual(migrate._deploy_summary(known),
                                "Stale hand-set deploy note")

    def test_stale_env_note_is_ignored_not_reserved(self):
        # version missing from the manifest AND the env note captured for a
        # DIFFERENT version -> honest None, never the stale note
        from unittest import mock

        from oikonome.db import migrate
        with mock.patch.dict(os.environ, {
                "OIKONOME_VERSION_NOTE": "Stale hand-set deploy note",
                "OIKONOME_VERSION_NOTE_FOR": "v0.1.2"}):
            self.assertIsNone(migrate._deploy_summary("v9.9.9"))
        # ...including when the marker is absent entirely (old .env)
        with mock.patch.dict(os.environ, {
                "OIKONOME_VERSION_NOTE": "Stale hand-set deploy note"}, clear=False):
            os.environ.pop("OIKONOME_VERSION_NOTE_FOR", None)
            self.assertIsNone(migrate._deploy_summary("v9.9.9"))

    def test_env_note_used_when_pinned_to_this_version(self):
        # manifest doesn't know the version, but the note was captured for
        # exactly this version (fresh install.sh run) -> note is trusted
        from unittest import mock

        from oikonome.db import migrate
        with mock.patch.dict(os.environ, {
                "OIKONOME_VERSION_NOTE": "the real subject",
                "OIKONOME_VERSION_NOTE_FOR": "v9.9.9"}):
            self.assertEqual(migrate._deploy_summary("v9.9.9"),
                             "the real subject")


class ExportNeedsConsentTests(_Base):
    """Settings tells every household "Oikonome support can never see your
    data unless you grant it." An export that merely RECORDS whether a
    consent exists and hands over the archive regardless makes that sentence
    false exactly where it counts."""

    def _tenant(self):
        conn = make_db()
        try:
            return str(conn.execute("SELECT current_setting('app.tenant_id') "
                                    "AS t").fetchone()["t"])
        finally:
            conn.close()

    def test_export_is_refused_without_a_live_consent(self):
        self._login()
        tid = self._tenant()
        r = self.client.post("/admin/console/tenant-export",
                             data={"tenant_id": tid})
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("application/zip",
                         r.headers.get("content-type", ""))
        self.assertIn("has not granted", r.text)
        self.assertTrue(_admin_rows(
            "SELECT 1 FROM admin_audit WHERE action='tenant_export_refused' "
            "AND target=%s", (tid,)),
            "a refusal must be audited too")

    def test_export_works_once_consent_is_granted(self):
        self._login()
        tid = self._tenant()
        admin = tenancy.admin_connect()
        try:
            admin.execute(
                "INSERT INTO support_consents (tenant_id, granted_by, "
                "reason, expires_at) VALUES (%s, %s, %s, now() + "
                "interval '1 hour')",
                (tid, "00000000-0000-0000-0000-000000000001", "debugging"))
        finally:
            admin.close()
        r = self.client.post("/admin/console/tenant-export",
                             data={"tenant_id": tid})
        self.assertEqual(r.status_code, 200)
        self.assertIn("zip", r.headers.get("content-type", ""))


class CommandFlagRaceTests(unittest.TestCase):
    """Two operators queueing a host command must not silently lose one.

    Checking `flag.exists()` and then, several statements later,
    `os.replace`ing the flag into place lets both halves of a double-submit
    pass the check, and the second replace overwrites the first request — so
    a command the console reports as queued simply never runs. The link()
    below IS the check: atomic, and it refuses when the flag is there.
    """

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.cmd = pathlib.Path(self.dir) / "cmd"
        self.cmd.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _queue(self, payload="first"):
        """The write half of run_command, in isolation."""
        import json
        import os
        flag = self.cmd / "command-requested"
        tmp = self.cmd / f".command-requested.{payload}.tmp"
        tmp.write_text(json.dumps({"cmd": payload}))
        try:
            os.link(tmp, flag)
            return True
        except FileExistsError:
            return False
        finally:
            tmp.unlink(missing_ok=True)

    def test_the_second_request_is_refused_not_silently_dropped(self):
        import json
        self.assertTrue(self._queue("first"))
        self.assertFalse(self._queue("second"),
                         "the second must be refused, not overwrite")
        landed = json.loads((self.cmd / "command-requested").read_text())
        self.assertEqual(landed["cmd"], "first",
                         "the first request must survive")

    def test_no_temp_files_are_left_behind_either_way(self):
        self._queue("first")
        self._queue("second")
        self.assertEqual(list(self.cmd.glob(".*.tmp")), [])

    def test_the_route_claims_the_flag_atomically(self):
        import inspect

        from oikonome.web import adminconsole
        src = inspect.getsource(adminconsole.run_command)
        self.assertIn("os.link(tmp, flag)", src)
        self.assertNotIn("if flag.exists():", src)


class SingleHouseholdTests(_Base):
    """A self-hosted instance does not take signups — `/signup` redirects
    to /login there — so the console must not offer the doors that lead to
    it. Offering them hands the operator a link that is dead on arrival,
    with nothing on the page to explain why."""

    def setUp(self):
        super().setUp()
        # the shared base runs as DEV_MODE, which IS multi-tenant; a real
        # self-host box is neither hosted nor dev
        self.appmod.DEV_MODE = False
        os.environ.pop("OIKONOME_HOSTED", None)

    def tearDown(self):
        self.appmod.DEV_MODE = True
        os.environ.pop("OIKONOME_HOSTED", None)
        super().tearDown()

    def test_the_signup_doors_are_absent_and_explained(self):
        self._login()
        r = self.client.get("/admin/console")
        self.assertEqual(r.status_code, 200)
        # match the SECTION HEADINGS, not the bare words: the console also
        # lists the release history, whose subjects may mention them
        for gone in ("<h2>Outstanding invites</h2>",
                     "<h2>Unverified users</h2>", "Mint an invite",
                     "Create an instance"):
            self.assertNotIn(gone, r.text, gone)
        # silence would read as a missing feature — say why, and where to go
        self.assertIn("single household", r.text)
        self.assertIn("Security &amp; household", r.text)

    def test_minting_an_invite_is_refused_not_quietly_dead(self):
        """The form is gone, but a stale tab or a curl must not mint a
        token whose link cannot work."""
        self._login()
        from oikonome.db import tenancy
        admin = tenancy.admin_connect()
        try:
            before = admin.execute(
                "SELECT count(*) AS n FROM signup_invites").fetchone()["n"]
        finally:
            admin.close()
        r = self.client.post("/admin/console/invite",
                             data={"email": "nobody@example.dev"},
                             follow_redirects=False)
        self.assertIn(r.status_code, (200, 303))
        admin = tenancy.admin_connect()
        try:
            after = admin.execute(
                "SELECT count(*) AS n FROM signup_invites").fetchone()["n"]
            self.assertEqual(after, before, "an invite was minted anyway")
        finally:
            admin.close()

    def test_a_hosted_instance_still_has_them(self):
        """No HTTP login here: the operator token cannot sign in on a
        hosted box (passkey-only), so assert the predicate
        the template gates on."""
        from oikonome.web import adminconsole as ac
        self.assertFalse(ac._multi_tenant())
        os.environ["OIKONOME_HOSTED"] = "1"
        self.assertTrue(ac._multi_tenant())


class HouseholdAgeLabelTests(unittest.TestCase):
    """The fleet's Age column reads at the precision a glance needs: days in
    the first month, whole months in the first year, then years to one
    decimal with a bare integer when the decimal is zero."""

    def test_precision_steps_with_age(self):
        import datetime as dt

        from oikonome.web.adminconsole import _age_label
        now = dt.datetime(2026, 9, 11, 12, tzinfo=dt.timezone.utc)
        def at(**kw):
            return _age_label(now - dt.timedelta(**kw), now)
        self.assertEqual(at(hours=3), "0d")
        self.assertEqual(at(days=3), "3d")
        self.assertEqual(at(days=29), "29d")
        self.assertEqual(at(days=30), "1mo")
        self.assertEqual(at(days=130), "4mo")
        self.assertEqual(at(days=364), "11mo")
        self.assertEqual(at(days=365), "1yr")
        self.assertEqual(at(days=548), "1.5yr")
        self.assertEqual(at(days=3653), "10yr")

    def test_naive_timestamps_are_read_as_utc(self):
        import datetime as dt

        from oikonome.web.adminconsole import _age_label
        now = dt.datetime(2026, 9, 11, tzinfo=dt.timezone.utc)
        self.assertEqual(_age_label(dt.datetime(2026, 9, 1), now), "10d")


if __name__ == "__main__":
    unittest.main()
