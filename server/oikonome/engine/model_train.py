"""Nightly retraining for the merchant categorizer — the loop-closer.

Without it, corrections are dead ends for the model: a fixed merchant
becomes a rule for THAT merchant, but the classifier (a read-only
deployment artifact) learns nothing from it, so the next lookalike
merchant falls through to the LLM — or, on an instance with no LLM, to
nothing. This module turns the household's accumulated answers into a
small per-tenant model, stored in the database (RLS, backed up, restored)
and consulted by model_categorize BEFORE the shipped artifact.

Training set discipline:
- `source='user'` rows are the gold — a human said so.
- `source='llm'` rows are included: they were answered by a model strong
  enough to be worth distilling into microsecond lookups.
- `source='model'` rows are EXCLUDED. Training the model on its own
  output is an echo chamber — confidence rises while information doesn't.
- seed/builtin rules are excluded too: they are already deterministic
  SQL, and the world-knowledge they encode is the shipped artifact's job.

The overlay abstains the same way the shipped model does, under its own
stricter bar (model_categorize.overlay_min_conf), so a small training
set yields a narrow model, not a wrong one — it answers near its own
merchants and stays silent elsewhere.
"""
from __future__ import annotations

import io
import logging

from . import merchant_sql, model_categorize

log = logging.getLogger(__name__)

# below this many labeled merchants (or fewer than two classes) a model
# would memorize, not generalize — the nightly job waits for more
MIN_SAMPLES = 50
MIN_CLASSES = 2
# …and above this many, the nightly fit stops growing: a bulk import can
# label tens of thousands of merchants in one day, and an uncapped
# training set buys that tenant unbounded worker CPU and artifact bytes
# every night inside the per-tenant sweep. The most recently answered
# two thousand merchants keep the overlay current on the vocabulary the
# household actually uses while bounding fit time to seconds and the
# compressed artifact to well under a megabyte.
MAX_SAMPLES = 2000


def training_pairs(conn) -> list[tuple[str, str]]:
    """(feature text, category) per labeled merchant. Feature text mirrors
    model_categorize.classify: the merchant plus one sample bank
    descriptor, so train and predict see the same shape of string.

    The sample descriptors come from ONE pass over transactions
    (DISTINCT ON keeps each merchant's newest name) joined to the rules —
    a correlated probe per merchant would rescan the whole table N times,
    since transactions carries no merchant_name index."""
    # the sample descriptor is looked up through the alias table: a rule is
    # keyed on a canonical / merchant name, a row on its raw descriptor, so
    # joining merchant_name = rule string misses every deduped merchant and
    # train and predict stop seeing the same shape of string. Every identity
    # form a rule can be keyed on (the merchant row's name, the
    # alias-resolved canonical, the raw aggregator name) is emitted per
    # transaction in ONE scan, then DISTINCT ON keeps each key's newest
    # descriptor. The obvious spelling — a LATERAL probe per rule with the
    # three forms OR'd together — is quadratic with no index to save it, and
    # on a large ledger it runs for hours inside the nightly sweep.
    rows = conn.execute(
        f"""WITH labeled AS (
               SELECT merchant, category_primary, classified_at
                 FROM merchant_categories
                WHERE source IN ('user', 'llm')
                  AND NOT disabled
                  AND category_primary <> ''
                ORDER BY classified_at DESC
                LIMIT %s),
           tx AS (
               SELECT t.name, t.date,
                      -- the merchant ROW first, exactly as the rule matcher
                      -- reaches a row: a Plaid-resolved transaction carries
                      -- its identity as an id, and matching only on strings
                      -- gave those merchants no sample descriptor at all
                      m.name AS row_name,
                      COALESCE(a.canonical, t.merchant_name, t.name)
                          AS canon_name,
                      t.merchant_name
                 FROM transactions t
                 LEFT JOIN merchants m ON m.id = t.merchant_id
                 LEFT JOIN merchant_canonical a
                        ON a.raw_merchant = {merchant_sql.RAW_KEY}
                WHERE t.removed = 0),
           latest AS (
               SELECT DISTINCT ON (key) key, name AS sample_name
                 FROM (SELECT row_name AS key, name, date FROM tx
                            WHERE row_name IS NOT NULL
                       UNION ALL
                       SELECT canon_name, name, date FROM tx
                       UNION ALL
                       SELECT merchant_name, name, date FROM tx
                            WHERE merchant_name IS NOT NULL) k
                ORDER BY key, date DESC)
           SELECT l.merchant, l.category_primary, s.sample_name
             FROM labeled l
             LEFT JOIN latest s ON s.key = l.merchant
            ORDER BY l.classified_at DESC""", (MAX_SAMPLES,)).fetchall()
    out = []
    for r in rows:
        feat = f"{r['merchant']} {r['sample_name'] or ''}".strip()
        out.append((feat, r["category_primary"]))
    return out


def _fit(pairs: list[tuple[str, str]]):
    """The same architecture the shipped artifact uses: character+word
    TF-IDF into a calibrated-ish logistic — predict_proba is what the
    abstention threshold reads, so the classifier must have one."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import FeatureUnion, Pipeline
    X = [p[0] for p in pairs]
    y = [p[1] for p in pairs]
    model = Pipeline([
        ("tfidf", FeatureUnion([
            ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5),
                                     min_df=1, sublinear_tf=True)),
            ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2),
                                     min_df=1, sublinear_tf=True)),
        ])),
        ("clf", LogisticRegression(max_iter=2000, C=4.0,
                                   class_weight="balanced")),
    ])
    model.fit(X, y)
    return model


def train(conn) -> dict:
    """Fit and store this tenant's overlay. Returns a stats dict; every
    refusal names its reason instead of half-training."""
    try:
        import joblib
        import sklearn
    except Exception as e:                       # noqa: BLE001
        # the optional [model] extra isn't installed — same clean absence
        # as the shipped artifact's loader
        return {"status": "unavailable", "reason": str(e)}
    pairs = training_pairs(conn)
    classes = {c for _, c in pairs}
    if len(pairs) < MIN_SAMPLES or len(classes) < MIN_CLASSES:
        # a labeled set that has SHRUNK below the bar (rules deleted or
        # disabled in bulk) must also retire any stored artifact: it was
        # trained on associations the household has since walked back, and
        # leaving it in place would keep serving those predictions forever
        retired = conn.execute("DELETE FROM categorizer_model").rowcount
        return {"status": "too-few", "samples": len(pairs),
                "classes": len(classes), "retired": bool(retired)}
    model = _fit(pairs)
    buf = io.BytesIO()
    joblib.dump(model, buf, compress=3)
    conn.execute(
        """INSERT INTO categorizer_model
               (artifact, trained_at, samples, classes, sklearn_version)
           VALUES (%s, now(), %s, %s, %s)
           ON CONFLICT (tenant_id) DO UPDATE SET
               artifact = EXCLUDED.artifact,
               trained_at = EXCLUDED.trained_at,
               samples = EXCLUDED.samples,
               classes = EXCLUDED.classes,
               sklearn_version = EXCLUDED.sklearn_version""",
        (model_categorize.seal_artifact(conn, buf.getvalue()),
         len(pairs), len(classes), sklearn.__version__))
    log.info("categorizer overlay trained: %d samples, %d classes, %d KB",
             len(pairs), len(classes), len(buf.getvalue()) // 1024)
    return {"status": "trained", "samples": len(pairs),
            "classes": len(classes), "bytes": len(buf.getvalue())}


def maybe_train(conn) -> dict:
    """The nightly entry: retrain only when the labeled set actually moved
    since the stored model — most nights this is one cheap count."""
    cur = conn.execute(
        """SELECT COUNT(*) AS n, MAX(classified_at) AS latest
             FROM merchant_categories
            WHERE source IN ('user', 'llm') AND NOT disabled
              AND category_primary <> ''""").fetchone()
    have = conn.execute(
        "SELECT trained_at, samples FROM categorizer_model").fetchone()
    # above the training cap the stored sample count sits at the cap, not
    # the raw rule count — compare against what a retrain would train on
    expect = min(cur["n"], MAX_SAMPLES)
    if have and have["samples"] == expect and (
            cur["latest"] is None or cur["latest"] <= have["trained_at"]):
        return {"status": "current", "samples": cur["n"]}
    return train(conn)
