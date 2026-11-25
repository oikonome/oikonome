"""External-database mode must work with any admin password the provider issued.

The migrate service carries the provider admin credential, and migrate,
`oikonome.sh backup`/`restore`/`reset` and `scripts/backup.sh` all connect
through its OIKONOME_ADMIN_DSN / OIKONOME_MAINT_DSN. Those are postgres://
URLs, so a password spliced into them raw breaks on '/', '@', '%', '#' or
':' — libpq then reads part of the password as the host, the database or an
escape, and every migrate and every backup fails. The password therefore
travels as PGPASSWORD (read by libpq, psql, pg_dump and psycopg) and the URLs
carry none: an explicit password in a DSN would win over PGPASSWORD.
"""

import os
import pathlib
import re
import unittest

import psycopg
import yaml
from psycopg.conninfo import conninfo_to_dict

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_OVERLAY = _ROOT / "docker" / "compose.external-db.yaml"

# every character that means something inside a URL's authority/path/query
_PASSWORD = "pa/ss@wo%rd#x:y?z"

_ENV = {
    "OIKONOME_DB_HOST": "db.example.internal",
    "OIKONOME_DB_PORT": "6543",
    "OIKONOME_DB_NAME": "ledger_db",
    "OIKONOME_DB_SSLMODE": "require",
    "OIKONOME_DB_ADMIN_USER": "dbadmin",
    "OIKONOME_DB_MAINT_DB": "maintdb",
    "POSTGRES_PASSWORD": _PASSWORD,
    "OIKONOME_APP_PASSWORD": "0123456789abcdef0123456789abcdef",
    "OIKONOME_ADMIN_PASSWORD": "fedcba9876543210fedcba9876543210",
}

_VAR = re.compile(r"\$\{(\w+)(?::?([-?])([^}]*))?\}")


def _interpolate(value: str, env: dict) -> str:
    """compose's ${VAR}, ${VAR:-default}, ${VAR:?message}."""
    def sub(m):
        name, op, arg = m.group(1), m.group(2), m.group(3)
        v = env.get(name)
        if v:
            return v
        if op == "-":
            return arg
        raise KeyError(f"{name} required: {arg}")
    return _VAR.sub(sub, value)


class _Loader(yaml.SafeLoader):
    """SafeLoader that reads compose's `!reset` merge tag as plain data."""


_Loader.add_constructor(
    "!reset", lambda loader, node: (
        loader.construct_mapping(node) if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)))


def _service_env(service: str, env: dict) -> dict:
    doc = yaml.load(_OVERLAY.read_text(), Loader=_Loader)
    raw = doc["services"][service]["environment"]
    return {k: _interpolate(str(v), env) for k, v in raw.items()}


class ExternalDbAdminPasswordTests(unittest.TestCase):
    def test_migrate_urls_carry_no_password_and_parse_to_the_provider(self):
        senv = _service_env("migrate", _ENV)
        self.assertEqual(senv.get("PGPASSWORD"), _PASSWORD)
        for key, db in (("OIKONOME_ADMIN_DSN", "ledger_db"),
                        ("OIKONOME_MAINT_DSN", "maintdb")):
            with self.subTest(key=key):
                self.assertNotIn(_PASSWORD, senv[key])
                parts = conninfo_to_dict(senv[key])
                self.assertNotIn("password", parts)
                self.assertEqual(parts.get("host"), "db.example.internal")
                self.assertEqual(parts.get("port"), "6543")
                self.assertEqual(parts.get("dbname"), db)
                self.assertEqual(parts.get("user"), "dbadmin")
                self.assertEqual(parts.get("sslmode"), "require")

    def test_runtime_urls_parse_with_the_installer_generated_passwords(self):
        # install.sh mints these as hex (URL-safe); the runtime DSNs splice
        # them, so the parse must hand them back intact
        for service in ("app", "worker"):
            senv = _service_env(service, _ENV)
            for key, pw in (("OIKONOME_DSN", _ENV["OIKONOME_APP_PASSWORD"]),
                            ("OIKONOME_ADMIN_DSN",
                             _ENV["OIKONOME_ADMIN_PASSWORD"])):
                with self.subTest(service=service, key=key):
                    parts = conninfo_to_dict(senv[key])
                    self.assertEqual(parts.get("password"), pw)
                    self.assertEqual(parts.get("host"), "db.example.internal")

    def test_installer_mints_url_safe_database_passwords(self):
        text = (_ROOT / "install.sh").read_text()
        self.assertRegex(text, r"rand\(\)\s*\{\s*openssl rand -hex \d+;\s*\}")
        for name in ("POSTGRES_PASSWORD", "OIKONOME_APP_PASSWORD",
                     "OIKONOME_ADMIN_PASSWORD"):
            self.assertIn(f'set_env {name} "$(rand)"', text)

    def test_a_server_accepts_the_password_through_pgpassword(self):
        # the migrate path end to end against the scratch server: the
        # rendered DSN pointed at it, the password only in PGPASSWORD
        admin = os.environ.get("OIKONOME_TEST_ADMIN_DSN",
                               "postgresql://postgres:devpass@127.0.0.1:5433")
        role = "oik_extdb_pw_probe"
        try:
            ctl = psycopg.connect(admin + "/postgres", autocommit=True,
                                  connect_timeout=5)
        except psycopg.OperationalError as e:
            self.skipTest(f"scratch server unreachable: {e}")
        target = conninfo_to_dict(admin + "/postgres")
        env = dict(_ENV, OIKONOME_DB_HOST=target["host"],
                   OIKONOME_DB_PORT=str(target.get("port", 5432)),
                   OIKONOME_DB_NAME="postgres", OIKONOME_DB_MAINT_DB="postgres",
                   OIKONOME_DB_SSLMODE="disable", OIKONOME_DB_ADMIN_USER=role)
        senv = _service_env("migrate", env)
        old = os.environ.get("PGPASSWORD")
        with ctl:
            ctl.execute(f"DROP ROLE IF EXISTS {role}")
            ctl.execute(f"CREATE ROLE {role} LOGIN PASSWORD "
                        + "'" + _PASSWORD.replace("'", "''") + "'")
            try:
                for key in ("OIKONOME_ADMIN_DSN", "OIKONOME_MAINT_DSN"):
                    with self.subTest(key=key):
                        os.environ["PGPASSWORD"] = senv["PGPASSWORD"]
                        with psycopg.connect(senv[key],
                                             connect_timeout=5) as c:
                            self.assertEqual(
                                c.execute("SELECT current_user").fetchone()[0],
                                role)
                        # and the server really checks it
                        os.environ["PGPASSWORD"] = "wrong"
                        with self.assertRaises(psycopg.OperationalError):
                            psycopg.connect(senv[key], connect_timeout=5)
            finally:
                if old is None:
                    os.environ.pop("PGPASSWORD", None)
                else:
                    os.environ["PGPASSWORD"] = old
                ctl.execute(f"DROP ROLE IF EXISTS {role}")


if __name__ == "__main__":
    unittest.main()
