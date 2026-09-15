"""`FLAG=0` in the env file must mean OFF, everywhere.

`os.environ.get(name)` used as a boolean reads the string `"0"` as TRUE, so
an operator who writes the most natural spelling of "off" in `docker/.env`
gets the opposite of what they asked for — and compose materializes every
declared key, so the value reaches the container. `envnum.env_flag` is the
one spelling that gets this right ("" is unset, and 0/false/off/no are off);
these are the flags where getting it wrong changes who can reach the app.
"""

import os
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

from oikonome.envnum import env_flag

from .util import _ensure_db

SERVER = Path(__file__).resolve().parents[1]


class EnvFlagSpellingTests(unittest.TestCase):
    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_off_spellings_read_as_off_and_unset_stays_unset(self):
        for v in ("0", "false", "False", "off", "no", "", "   "):
            os.environ["OIKONOME_TMP_FLAG_PROBE"] = v
            self.assertFalse(env_flag("OIKONOME_TMP_FLAG_PROBE"), repr(v))
        for v in ("1", "true", "TRUE", "on", "yes"):
            os.environ["OIKONOME_TMP_FLAG_PROBE"] = v
            self.assertTrue(env_flag("OIKONOME_TMP_FLAG_PROBE"), repr(v))
        os.environ.pop("OIKONOME_TMP_FLAG_PROBE", None)
        self.assertFalse(env_flag("OIKONOME_TMP_FLAG_PROBE"))


class OpenSignupGateTests(unittest.TestCase):
    """The worst one: the invite gate is what keeps hosted signup off the
    open internet, and `OIKONOME_OPEN_SIGNUP=0` read as true opens it."""

    @classmethod
    def setUpClass(cls):
        os.environ["OIKONOME_DEV"] = "1"
        _ensure_db()
        import oikonome.web.app as appmod
        appmod.DEV_MODE = True
        from fastapi.testclient import TestClient
        cls.client = TestClient(appmod.app)

    def setUp(self):
        os.environ["OIKONOME_HOSTED"] = "1"
        from oikonome.web import security
        security._limiter._hits.clear()          # 5/h signup bucket

    def tearDown(self):
        os.environ.pop("OIKONOME_HOSTED", None)
        os.environ.pop("OIKONOME_OPEN_SIGNUP", None)

    def _signup(self):
        return self.client.post(
            "/api/signup",
            data={"email": f"of-{uuid.uuid4().hex[:8]}@x.dev",
                  "password": "correct-horse-battery", "invite": ""})

    def test_open_signup_set_to_zero_keeps_the_invite_gate_shut(self):
        os.environ["OIKONOME_OPEN_SIGNUP"] = "0"
        self.assertEqual(403, self._signup().status_code,
                         "OIKONOME_OPEN_SIGNUP=0 opened hosted signup")
        os.environ["OIKONOME_OPEN_SIGNUP"] = ""      # unset via compose
        self.assertEqual(403, self._signup().status_code)

    def test_open_signup_still_opens_when_actually_set(self):
        os.environ["OIKONOME_OPEN_SIGNUP"] = "1"
        self.assertEqual(200, self._signup().status_code)


class OpenApiExposureTests(unittest.TestCase):
    """The schema maps every route, including the sensitive ones, so it is
    served on dev instances only. `OIKONOME_DEV=0` is a production box."""

    def _openapi_url(self, dev_value):
        env = dict(os.environ)
        env["OIKONOME_DEV"] = dev_value
        env.pop("OIKONOME_MASTER_KEY", None)
        # a fresh interpreter: the gate is evaluated once, at import
        r = subprocess.run(
            [sys.executable, "-c",
             "from oikonome.web.app import app; print(app.openapi_url)"],
            capture_output=True, text=True, env=env, cwd=str(SERVER))
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        return r.stdout.strip()

    def test_zero_does_not_publish_the_schema(self):
        self.assertEqual("None", self._openapi_url("0"),
                         "OIKONOME_DEV=0 published /openapi.json")

    def test_dev_still_publishes_the_schema(self):
        self.assertEqual("/openapi.json", self._openapi_url("1"))


class AdminConsoleReachabilityTests(unittest.TestCase):
    """The console is the operator's whole back door — tenant lookup and
    host actions. `OIKONOME_ADMIN_ENABLED=0` reading as true stands it
    up on an instance whose operator asked for no console at all, and with
    no token configured that door has no credential in front of it."""

    def setUp(self):
        self._saved = dict(os.environ)
        for k in ("OIKONOME_ADMIN_TOKEN", "OIKONOME_ADMIN_ENABLED",
                  "OIKONOME_ADMIN_REQUIRE_PASSKEY"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_console_stays_off_when_enabled_is_zero(self):
        from oikonome.web import adminconsole
        os.environ["OIKONOME_ADMIN_ENABLED"] = "1"
        self.assertTrue(adminconsole._enabled(), "precondition: =1 is on")
        os.environ["OIKONOME_ADMIN_ENABLED"] = "0"
        self.assertFalse(adminconsole._enabled(),
                         "OIKONOME_ADMIN_ENABLED=0 stood the console up")
        os.environ["OIKONOME_ADMIN_ENABLED"] = ""
        self.assertFalse(adminconsole._enabled())

    def test_passkey_requirement_off_when_set_to_zero(self):
        from oikonome.web import adminconsole
        os.environ["OIKONOME_ADMIN_REQUIRE_PASSKEY"] = "1"
        self.assertTrue(adminconsole._require_passkey(),
                        "precondition: =1 demands a passkey")
        os.environ["OIKONOME_ADMIN_REQUIRE_PASSKEY"] = "0"
        self.assertFalse(adminconsole._require_passkey(),
                         "=0 still forced the passkey-only posture")


class DemoResetTargetTests(unittest.TestCase):
    """The hourly demo job DESTROYS the tenant it runs against and re-seeds
    from scratch. `OIKONOME_DEMO=0` means 'this is not a demo', and reading
    it as true points that job at a real instance's data."""

    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_zero_is_not_a_demo(self):
        import asyncio as _aio

        from oikonome.jobs import worker
        for off in ("0", "false", "", "no"):
            os.environ["OIKONOME_DEMO"] = off
            self.assertEqual(
                "not-a-demo", _aio.run(worker.demo_reset_all(None)),
                f"OIKONOME_DEMO={off!r} let the destructive reset run")


class AutoRebootTests(unittest.TestCase):
    """The nightly check schedules an unattended host restart. An operator
    who wrote `OIKONOME_AUTO_REBOOT=0` asked for no such thing, and reading
    it as true would still consult host health and, on a pending reboot,
    broadcast a countdown and drop the ops-cmd flag."""

    def setUp(self):
        self._saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._saved)

    def test_zero_keeps_the_nightly_reboot_off(self):
        import tempfile

        from oikonome.jobs import worker
        with tempfile.TemporaryDirectory() as d:
            # Point host-health at an empty state dir: an enabled check
            # returns "no-host-health", a disabled one never looks.
            os.environ["OIKONOME_STATE_DIR"] = d
            os.environ["OIKONOME_AUTO_REBOOT"] = "1"
            self.assertEqual("no-host-health", worker._nightly_reboot_check(),
                             "precondition: =1 enables the check")
            for off in ("0", "false", "", "no"):
                os.environ["OIKONOME_AUTO_REBOOT"] = off
                self.assertEqual(
                    "disabled", worker._nightly_reboot_check(),
                    f"OIKONOME_AUTO_REBOOT={off!r} left auto-reboot on")


if __name__ == "__main__":
    unittest.main()
