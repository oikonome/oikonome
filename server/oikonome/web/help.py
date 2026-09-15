"""in-app help — the docs/*.md the repo ships, served as JSON
topics for the SPA's Help page (single source: the GitHub copies stay
the canonical files; the container carries them at <root>/docs). The
FAQ is docs/faq.md like everything else — deliberately brief answers to
the stumbles people actually hit, never a second docs tree."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, Depends

router = APIRouter(prefix="/api")


def _docs_dir() -> Path:
    """Where the shipped docs live. Repo layout: three levels up from web/
    (…/app/server/oikonome/web → …/app/docs). The CONTAINER is different —
    `pip install ./server` puts this file in site-packages, so the relative
    walk lands in /usr/local/lib/… and finds nothing, which makes in-app
    Help serve ZERO topics on a container install, invisibly to a test suite
    run from the repo layout. The image COPYs docs to /app/docs — probe both,
    and let OIKONOME_DOCS_DIR override for exotic layouts."""
    env = os.environ.get("OIKONOME_DOCS_DIR")
    if env:
        return Path(env)
    repo = Path(__file__).parents[3] / "docs"
    for cand in (repo, Path("/app/docs")):
        if cand.is_dir():
            return cand
    return repo


DOCS_DIR = _docs_dir()

# curated order — the reading path a new user actually takes; anything
# not listed sorts after, alphabetically
_ORDER = ["faq", "quickstart", "importing", "troubleshooting",
          "reverse-proxy", "community-scripts", "collectors",
          "tax-documents"]

# docs/guides/*.md are per-feature product guides, listed as
# their own group after the root docs (the SPA renders the groups; the
# flat order here just keeps faq/quickstart first for older clients).
# Ordered as a reading path: the verdict first, then the money model,
# then the pages, then the trust/admin topics.
_GUIDE_ORDER = ["today", "lenses", "concepts", "budget", "bills",
                "money-map", "cash-flow", "transactions", "merchants", "rules",
                "net-worth", "retirement", "accounts",
                "owners", "reimbursements", "receipts", "business", "alerts",
                "assistant", "security", "import-history"]

# root docs split into sidebar groups; guides are group "guides"
_SETUP = {"quickstart", "importing", "collectors", "community-scripts",
          "reverse-proxy", "deployment-modes"}


def _user():
    from .app import current_user
    return current_user


def _title(md: str, fallback: str) -> str:
    for line in md.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    # no H1 (a stub that only points elsewhere): the stem, read as words
    return fallback.replace("-", " ").capitalize()


# The shipped docs cannot know an instance's support address, so they say
# this; /help swaps in the operator's address when one is configured.
SUPPORT_PHRASE = "your instance's support address"


def _localize(md: str) -> str:
    """Name this instance's support address where the docs say
    "your instance's support address". An instance with none configured
    keeps the phrase — better than a placeholder that reads as a bug."""
    from .app import support_email
    here = support_email()
    return md.replace(SUPPORT_PHRASE, here) if here else md


def _load(folder: Path, group_of) -> dict:
    found = {}
    if folder.is_dir():
        for f in folder.glob("*.md"):
            try:
                body = f.read_text(encoding="utf-8")
            except OSError:
                continue
            body = _localize(body)
            found[f.stem] = {"id": f.stem, "title": _title(body, f.stem),
                             "body": body, "group": group_of(f.stem)}
    return found


@router.get("/help")
def help_topics(user: dict = Depends(_user())):
    docs = _load(DOCS_DIR,
                 lambda s: "setup" if s in _SETUP else "reference")
    guides = _load(DOCS_DIR / "guides", lambda s: "guides")
    ordered = [docs.pop(k) for k in _ORDER if k in docs]
    ordered += [docs[k] for k in sorted(docs)]
    ordered += [guides.pop(k) for k in _GUIDE_ORDER if k in guides]
    ordered += [guides[k] for k in sorted(guides)]
    return {"topics": ordered}
