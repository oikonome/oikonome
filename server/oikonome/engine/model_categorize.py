"""Local merchant classifier — the categorization fallback when the
aggregator doesn't know.

A small supervised text model (TF-IDF character+word n-grams into a
logistic classifier) maps a merchant descriptor to one of the spend
primaries, with a real probability. It exists because measuring the
prompted-LLM approach showed a tiny trained classifier matching a 4B
instruct model on the same eval while costing megabytes and
microseconds instead of gigabytes and seconds — and because hosted
deployments run no LLM at all, so without it their only machine
categorization is the seed list.

The model file is DEPLOYMENT-PROVIDED, never shipped in this repo:
`OIKONOME_CATEGORIZER_MODEL` points at a joblib artifact (the standard
compose mounts ./models read-only). Unset or missing = the feature does
not exist, exactly like an unconfigured LLM backend.

Trust discipline is identical to every other machine source: rules are
written to merchant_categories under source='model', which puts them
under user rules and behind apply()'s trust guard — a model answer can
refine a generic aggregator bucket but never move a row out of a
concrete Plaid family. Below-confidence predictions are ABSTENTIONS:
the merchant is left for the LLM (if one is configured) or simply left
alone, because "no answer" beats a confident-sounding guess — the knob
a prompted model never offers honestly.
"""
from __future__ import annotations

import logging
import os
import threading
from collections import OrderedDict

from cryptography.fernet import InvalidToken

from ..db import crypto

log = logging.getLogger(__name__)

# below this max-probability the model abstains. Tuned against the
# eval sets; env-tunable so a deployment can trade coverage
# for precision without a release.
DEFAULT_MIN_CONF = 0.55
# the learned overlay writes durable rules from a much smaller training
# set than the shipped artifact, so it holds a stricter bar: on held-out
# corrections the higher threshold trades some coverage for noticeably
# better precision. What it abstains on still reaches the shipped model and
# the LLM, so the higher bar costs little and keeps bad rules out of the
# ledger.
DEFAULT_OVERLAY_MIN_CONF = 0.70

_lock = threading.Lock()
_cache: dict = {"path": None, "model": None}
# per-tenant learned overlays (engine/model_train.py), keyed by tenant and
# the row's trained_at so a nightly retrain is picked up on the next call.
# Bounded LRU: this cache lives in a long-lived worker process serving an
# unbounded number of tenants (hosted deployments), so without a cap every
# tenant that ever syncs would pin its deserialized overlay in memory
# forever — a slow leak. The cap keeps the hot tenants resident; a cold
# tenant that gets evicted just pays one artifact reload on its next sync.
_TENANT_CACHE_MAX = 256
_tenant_cache: OrderedDict = OrderedDict()


def _path() -> str:
    return os.environ.get("OIKONOME_CATEGORIZER_MODEL", "")


def min_conf() -> float:
    try:
        return float(os.environ.get("OIKONOME_CATEGORIZER_MIN_CONF", "")
                     or DEFAULT_MIN_CONF)
    except ValueError:
        return DEFAULT_MIN_CONF


def overlay_min_conf() -> float:
    try:
        return float(os.environ.get("OIKONOME_CATEGORIZER_OVERLAY_MIN_CONF",
                                    "") or DEFAULT_OVERLAY_MIN_CONF)
    except ValueError:
        return DEFAULT_OVERLAY_MIN_CONF


def available() -> bool:
    p = _path()
    if not p or not os.path.exists(p):
        return False
    # a configured path whose load already failed (no sklearn on this box,
    # corrupt artifact) is NOT available — the Settings card reads this, and
    # it must not claim a classifier the process cannot actually run
    return not (_cache["path"] == p and _cache["model"] is None)


def _load():
    """Lazy, cached, thread-safe load. A worker process serves many
    tenants; the artifact is tens of MB and loads once. A load failure is
    cached too (as None) so an install without the optional sklearn stack
    logs the reason once, not once per sync."""
    p = _path()
    with _lock:
        if _cache["path"] == p:
            return _cache["model"]
        try:
            # sklearn/joblib are the optional `oikonome[model]` extra — a
            # self-host box without them simply doesn't get the backend
            import joblib
            model = joblib.load(p)
            log.info("categorizer model loaded from %s (classes: %d)",
                     p, len(getattr(model, "classes_", [])))
        except Exception as e:                   # noqa: BLE001
            log.warning("model backend unavailable: %s: %s",
                        type(e).__name__, e)
            model = None
        _cache["path"] = p
        _cache["model"] = model
        return model


# The overlay is a pickle, and unpickling is code execution: whatever can
# write categorizer_model.artifact (the app role, or SQL injected through
# it) could plant a gadget that the worker runs at its next categorize
# pass. So an artifact is only ever unpickled if it authenticates under
# THIS tenant's data key — the same Fernet envelope (AES-CBC + HMAC-SHA256)
# the bank tokens use, whose key sits wrapped under the master key and so
# cannot be recovered from the database alone. model_train seals at save
# time; tenant_model opens at load. A blob with no envelope, or one whose
# tag does not verify, is refused and the row cleared so the nightly
# retrain rebuilds it under a proper seal.
#
# With no master key configured there is no tenant key to seal under; that
# dev-mode keeps crypto.decrypt's contract (untagged values pass through,
# a tagged value with no key to check it is an error) — every secret in
# such an install is plaintext already, so nothing is lost that was held.
_SEAL_PREFIX = b"okm:v1:"


def seal_artifact(conn, raw: bytes) -> bytes:
    """The bytes to store for a freshly pickled overlay."""
    f = crypto._tenant_fernet(conn)
    if f is None:
        return raw
    return _SEAL_PREFIX + f.encrypt(raw)


def open_artifact(conn, stored) -> bytes | None:
    """The pickled bytes if the stored blob authenticates, else None."""
    blob = bytes(stored)
    f = crypto._tenant_fernet(conn)
    if not blob.startswith(_SEAL_PREFIX):
        return blob if f is None else None
    if f is None:
        return None
    try:
        return f.decrypt(blob[len(_SEAL_PREFIX):])
    except InvalidToken:
        return None


def tenant_model(conn):
    """This tenant's learned overlay, if one has been trained — cached by
    (tenant, trained_at) so a nightly retrain replaces it on the next
    call. The cache probe reads only the key columns: this runs on every
    categorize pass (hourly per synced tenant), and the artifact BYTEA is
    hundreds of KB that only changes nightly, so the blob crosses the
    wire on a miss and the hit path costs one tiny row read.

    An artifact pickled under a different sklearn than this process runs
    is REFUSED and cleared, not loaded: a version-skewed pickle can
    deserialize fine and still predict garbage, and clearing the row lets
    the nightly retrain rebuild it under the running version."""
    try:
        row = conn.execute(
            "SELECT tenant_id, trained_at FROM categorizer_model").fetchone()
    except Exception:                            # noqa: BLE001
        return None                              # pre-migration database
    if not row:
        return None
    key = (str(row["tenant_id"]), row["trained_at"])
    with _lock:
        if key in _tenant_cache:
            _tenant_cache.move_to_end(key)   # a hit refreshes LRU recency
            return _tenant_cache[key]
    # miss: fetch the artifact, re-reading the key columns in the same
    # statement so a retrain landing between the two reads keys the cache
    # to the artifact actually loaded
    full = conn.execute(
        "SELECT tenant_id, artifact, trained_at, sklearn_version "
        "FROM categorizer_model").fetchone()
    if not full:
        return None
    stored_ver = full["sklearn_version"] or ""
    if stored_ver:
        try:
            import sklearn
            running = sklearn.__version__
        except Exception:                        # noqa: BLE001
            running = None                       # joblib.load fails below
        if running is not None and stored_ver != running:
            log.warning(
                "tenant categorizer overlay refused: trained under "
                "sklearn %s, running %s — clearing so the nightly "
                "retrain rebuilds it", stored_ver, running)
            conn.execute("DELETE FROM categorizer_model")
            return None
    key = (str(full["tenant_id"]), full["trained_at"])
    raw = open_artifact(conn, full["artifact"])
    if raw is None:
        log.warning(
            "tenant categorizer overlay refused: artifact does not "
            "authenticate under the tenant key — clearing so the nightly "
            "retrain rebuilds it")
        conn.execute("DELETE FROM categorizer_model")
        return None
    try:
        import io

        import joblib
        model = joblib.load(io.BytesIO(raw))
    except Exception as e:                       # noqa: BLE001
        log.warning("tenant categorizer overlay unavailable: %s: %s",
                    type(e).__name__, e)
        model = None
    with _lock:
        # one entry per tenant: drop this tenant's older trained_at keys
        for k in [k for k in _tenant_cache if k[0] == key[0]]:
            del _tenant_cache[k]
        _tenant_cache[key] = model
        while len(_tenant_cache) > _TENANT_CACHE_MAX:
            _tenant_cache.popitem(last=False)   # evict least recently used
        return model


def _predict(model, merchants: list[dict],
             thr: float | None = None) -> dict[int, tuple[str, float]]:
    feats = [f"{m.get('raw_name') or m['merchant']} "
             f"{m.get('sample_name') or ''}".strip()
             for m in merchants]
    proba = model.predict_proba(feats)
    classes = list(model.classes_)
    thr = min_conf() if thr is None else thr
    out: dict[int, tuple[str, float]] = {}
    for i, row in enumerate(proba):
        j = int(row.argmax())
        conf = float(row[j])
        if conf >= thr:
            out[i] = (str(classes[j]), conf)
    return out


def classify(merchants: list[dict], conn=None,
             stats: dict | None = None) -> dict[int, tuple[str, float]]:
    """{index → (category, confidence)} for the CONFIDENT subset of
    `merchants` (pending_merchants dicts). Abstentions are simply absent.
    Feature text mirrors training: the raw descriptor plus the sample
    bank string.

    With a connection, the tenant's learned overlay answers FIRST — it was
    trained on this household's own corrections, so near its merchants it
    outranks the world-knowledge artifact; where it abstains, the shipped
    model gets the same chance it always had. Either model may be absent
    (no overlay trained yet; no artifact mounted) without costing the
    other its turn. `stats`, when given, receives per-source admission
    counts ('overlay'/'shipped') — the two sources hold DIFFERENT
    confidence bars, so a caller logging its writes needs to know which
    bar admitted what."""
    if not merchants:
        return {}
    out: dict[int, tuple[str, float]] = {}
    if conn is not None:
        overlay = tenant_model(conn)
        if overlay is not None:
            try:
                out = _predict(overlay, merchants,
                               thr=overlay_min_conf())
            except Exception as e:               # noqa: BLE001
                log.warning("tenant overlay predict failed: %s: %s",
                            type(e).__name__, e)
                out = {}
    if stats is not None:
        stats["overlay"] = len(out)
        stats["shipped"] = 0
    if not available():
        return out
    model = _load()
    if model is None:
        return out
    rest = [m for i, m in enumerate(merchants) if i not in out]
    idx = [i for i in range(len(merchants)) if i not in out]
    if not rest:
        return out
    try:
        got = _predict(model, rest)
    except Exception as e:                       # noqa: BLE001
        # a version-skewed pickle can load fine and still fail at predict;
        # degrade to "feature absent" (cached, so it logs once), never take
        # categorization down with it
        log.warning("model backend unavailable: %s: %s", type(e).__name__, e)
        with _lock:
            _cache["model"] = None
        return out
    if stats is not None:
        stats["shipped"] = len(got)
    for k, v in got.items():
        out[idx[k]] = v
    return out
