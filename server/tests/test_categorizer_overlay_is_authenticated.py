"""The learned categorizer overlay is a pickle, and unpickling runs code.

Anything that can write categorizer_model.artifact — the app role, or SQL
injected through it — could plant a gadget the worker executes at its
next categorize pass. So the artifact only ever reaches joblib.load if it
authenticates under the tenant's own data key: model_train seals at save
time, tenant_model verifies at load. A blob with no seal, or a seal that
does not verify, is refused and cleared so the nightly retrain rebuilds it.
"""

import os
import sys
import types
import unittest
from unittest import mock

from cryptography.fernet import Fernet

from oikonome.engine import model_categorize, model_train

from .util import make_db

try:
    import sklearn                                       # noqa: F401
    HAVE_SKLEARN = True
except Exception:                                        # noqa: BLE001
    HAVE_SKLEARN = False


class _JoblibSpy:
    """Stand-in joblib that records the bytes it was asked to unpickle."""

    def __init__(self):
        self.seen: list[bytes] = []

    def load(self, f):
        self.seen.append(f.read())
        return object()


class OverlayAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(
            os.environ, {"OIKONOME_MASTER_KEY": Fernet.generate_key().decode()})
        self._env.start()
        self.conn = make_db()
        self.spy = _JoblibSpy()
        stub = types.ModuleType("joblib")
        stub.load = self.spy.load
        self._mods = mock.patch.dict(sys.modules, {"joblib": stub})
        self._mods.start()
        model_categorize._tenant_cache.clear()

    def tearDown(self):
        self._mods.stop()
        model_categorize._tenant_cache.clear()
        self.conn.close()
        self._env.stop()

    def _store(self, blob: bytes, trained_at_offset: str = "0 hours"):
        self.conn.execute(
            f"""INSERT INTO categorizer_model (artifact, trained_at, samples,
                    classes, sklearn_version)
                VALUES (%s, now() - interval '{trained_at_offset}', 60, 3, '')
                ON CONFLICT (tenant_id) DO UPDATE SET
                    artifact = EXCLUDED.artifact,
                    trained_at = EXCLUDED.trained_at""", (blob,))

    def _row_present(self) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM categorizer_model").fetchone() is not None

    def test_sealed_artifact_loads_its_payload(self):
        self._store(model_categorize.seal_artifact(self.conn, b"pickled"))
        self.assertIsNotNone(model_categorize.tenant_model(self.conn))
        self.assertEqual(self.spy.seen, [b"pickled"])

    def test_tampered_artifact_is_refused_and_cleared(self):
        sealed = bytearray(model_categorize.seal_artifact(self.conn, b"pickled"))
        # flip a byte inside the ciphertext, past the prefix and Fernet header
        sealed[-10] ^= 0x01
        self._store(bytes(sealed))
        self.assertIsNone(model_categorize.tenant_model(self.conn))
        self.assertEqual(self.spy.seen, [], "tampered pickle reached joblib")
        self.assertFalse(self._row_present(),
                         "refused artifact left in place for the next pass")

    def test_unsealed_artifact_is_untrusted_when_a_key_exists(self):
        self._store(b"opaque-bytes")
        self.assertIsNone(model_categorize.tenant_model(self.conn))
        self.assertEqual(self.spy.seen, [], "unsealed pickle reached joblib")
        self.assertFalse(self._row_present())

    def test_seal_from_another_tenant_key_does_not_verify(self):
        other = make_db()
        try:
            self._store(model_categorize.seal_artifact(other, b"pickled"))
        finally:
            other.close()
        self.assertIsNone(model_categorize.tenant_model(self.conn))
        self.assertEqual(self.spy.seen, [])

    def test_refusal_is_not_cached(self):
        """A refused blob must not poison the cache: the retrain that
        follows lands under a new trained_at and is loaded normally."""
        self._store(b"opaque-bytes", "2 hours")
        self.assertIsNone(model_categorize.tenant_model(self.conn))
        self._store(model_categorize.seal_artifact(self.conn, b"fresh"))
        self.assertIsNotNone(model_categorize.tenant_model(self.conn))
        self.assertEqual(self.spy.seen, [b"fresh"])

    @unittest.skipUnless(HAVE_SKLEARN, "optional [model] extra not installed")
    def test_training_stores_a_sealed_artifact(self):
        self._mods.stop()                       # real joblib for the fit
        try:
            for i in range(30):
                for merchant, cat in (
                        (f"Cafe Ristorante {i}", "FOOD_AND_DRINK"),
                        (f"Clinic Dermatology {i}", "MEDICAL")):
                    self.conn.execute(
                        """INSERT INTO merchant_categories (merchant,
                               category_primary, source)
                           VALUES (%s, %s, 'user')""", (merchant, cat))
            self.assertEqual("trained", model_train.train(self.conn)["status"])
            blob = bytes(self.conn.execute(
                "SELECT artifact FROM categorizer_model").fetchone()["artifact"])
            self.assertTrue(blob.startswith(model_categorize._SEAL_PREFIX),
                            "trained overlay stored without a seal")
            self.assertIsNotNone(model_categorize.tenant_model(self.conn))
        finally:
            self._mods.start()


class OverlayWithoutMasterKeyTests(unittest.TestCase):
    """Dev mode without a master key has no tenant key to seal under; it
    keeps crypto.decrypt's contract — untagged passes through, a tagged
    value that cannot be checked is refused."""

    def setUp(self):
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop("OIKONOME_MASTER_KEY", None)
        self.conn = make_db()
        self.spy = _JoblibSpy()
        stub = types.ModuleType("joblib")
        stub.load = self.spy.load
        self._mods = mock.patch.dict(sys.modules, {"joblib": stub})
        self._mods.start()
        model_categorize._tenant_cache.clear()

    def tearDown(self):
        self._mods.stop()
        model_categorize._tenant_cache.clear()
        self.conn.close()
        self._env.stop()

    def test_sealed_artifact_cannot_be_verified_so_is_refused(self):
        self.conn.execute(
            """INSERT INTO categorizer_model (artifact, samples, classes,
                   sklearn_version) VALUES (%s, 60, 3, '')""",
            (model_categorize._SEAL_PREFIX + b"cannot-check-this",))
        self.assertIsNone(model_categorize.tenant_model(self.conn))
        self.assertEqual(self.spy.seen, [])


if __name__ == "__main__":
    unittest.main()
