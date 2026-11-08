"""Which account a source name lands on: the account_sources table.

A hand-made or imported account's id is opaque (`manual:<letters>`, or
`manual:<importer>-<letters>` for one a multi-account file split off),
minted once and never derived from its name. What a door used to re-derive
from the name is looked up here instead: one row per (door, source) says
which account that source fills. The source is the name as the door was
given it — a typed new-account name, a file's account name, a split file's
source account — and the row holds only an HMAC of it under the
household's own secret (base.txn_id_secret), never the name. So a source
spelling a full card number is told apart from another card sharing its
last four without the number being kept anywhere, and the account can be
renamed, its stored name cut, or its id moved by a re-key without the
source losing its account.

Kinds of row, by the prefix of `source_hmac`; each keyed one also names
the secret it was written under (key_mark):

  s:  keyed, over the source as the door was given it (whitespace folded;
      a split file's folded in case too, as base._split_source compares
      it), every full number in it spelled as its bare digits, so a card
      typed in groups or not is one source. The lookup: one account each;
  d:  keyed, over those digits alone, beside the s-row of a source that
      spells a full number: the card. An account answering to one card's
      d-row is never the account of another card, however alike their cut
      names (claimed_by_another);
  c:  unkeyed SHA-256 of an account's stored name with every full
      number cut (card_number.cut), written beside every hand-made
      account (migration 207 backfilled one per existing account). It is
      never taken over a name that still spells a number: the digest
      rides every export, and one over a whole card number whose last
      four the name shows is a short search from the number. A stored
      name is cut and rides the export beside the row, so the digest
      discloses nothing the archive does not; it is what still leads to
      an account after a restore into another household, whose secret
      differs, and it is the candidate list when two cards share a cut
      name. It carries no key mark, deliberately: what it says — this
      account's stored name hashes to this — is checked against the
      account it names, on any instance, so it counts only while that
      holds (by_name), and a row an archive brought, or one a cut, merge
      or move left naming a name its account no longer stores, is no
      claim and is dropped (backfill, and every `oikonome migrate`).

The secret never leaves the instance, so a restore elsewhere brings keyed
rows nothing here can match, and re-keying them would need the source
names the table exists not to keep. Such a row is dead weight (its key
mark says so, so it is never taken for a claim): the source finds its
account the way an account made before this table did — by its stored
name, or where an earlier build's derived id put it — asked once when
that leads to more than one, and the answer is keyed anew here.

The c-row digest lives in SQL twice: migration 207 (sealed) and
NAME_DIGEST_SQL below, which `backfill` runs (a restore, a cut, and
`oikonome migrate` household by household). Keep them in step; SQL takes it only over a
name the numbered-name selector does not match once read the way a
slug reads it (FOLDED_SQL), which the cut leaves as it is, and
_name_digest folds before it cuts, so SQL and _name_digest agree.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import string

from . import card_number

#: the door every hand-made account answers to: the folder and single-file
#: imports' new-account names and the account-add doors
DOOR_MANUAL = "manual"

#: the split importers whose ids carry their own prefix
SPLIT_PREFIXES = ("qif", "mint", "monarch", "copilot", "simplifi", "ofx")

_LOCK_KEY = "oikonome:ensure-account:"
_ID_LETTERS = string.ascii_lowercase
_ID_LEN = 16
_WS_RE = re.compile(r"[ \t\n\r\f\v]+")

# The c-row digest, as SQL: whitespace runs folded to one space, trimmed,
# SHA-256 in hex. Python's twin is _name_digest.
NAME_DIGEST_SQL = ("'c:' || encode(sha256(convert_to(btrim(regexp_replace("
                   "{expr}, '[ \\t\\n\\r\\f\\v]+', ' ', 'g'), ' '),"
                   " 'UTF8')), 'hex')")

# A name read the way an account id's slug reads it: every run of
# punctuation or whitespace between two digits is one space
# (card_number.cut_as_slug_reads). A number whose groups a tab, an
# underscore, a comma or a dash of any width separates is still a
# number, so the numbered-name selector is always run against this, never
# against the raw name; it only ever shortens a separator to one the
# selector takes, so it selects every name the plain selector does.
FOLDED_SQL = ("regexp_replace({expr}, '(?<=[0-9])[^0-9A-Za-z]+(?=[0-9])',"
              " ' ', 'g')")

# Every hand-made account a c-row is written for: not a split importer's,
# which the split door finds through the rows it holds.
# (in statements run with parameters, so a literal percent is doubled)
HAND_MADE_SQL = (r"a.item_id = 'manual' AND a.id LIKE 'manual:%%'"
                 r" AND a.id !~ '^manual:(qif|mint|monarch|copilot|simplifi"
                 r"|ofx)-' AND btrim(coalesce(a.name, '')) <> ''")

# A hand-made account `a` whose stored name is cut: its folded name
# matches no numbered-name selector, %(p)s. One still spelling a number
# gets its c-row once `oikonome migrate` or a restore has cut it: both
# select names read this way (base.NUMBERED_NAMES_WHERE) and cut them as
# the doors store them (stored_account_name), so no name this guard
# holds back is one they leave whole.
_CUT_HAND_MADE_SQL = (HAND_MADE_SQL + " AND " + FOLDED_SQL.format(expr="a.name")
                      + " !~ %(p)s")

# A c-row for every such account.
BACKFILL_SQL = (
    "INSERT INTO account_sources (tenant_id, door, source_hmac, account_id)"
    " SELECT a.tenant_id, 'manual', " + NAME_DIGEST_SQL.format(expr="a.name")
    + ", a.id FROM accounts a WHERE " + _CUT_HAND_MADE_SQL
    + " ON CONFLICT DO NOTHING")

# A c-row `{row}` naming account `{aid}` is true: the digest of the cut
# stored name of the hand-made account it names.
_C_ROW_TRUE_SQL = ("a.id = {aid} AND " + _CUT_HAND_MADE_SQL
                   + " AND {row} = " + NAME_DIGEST_SQL.format(expr="a.name"))

# Every c-row that is not true: its account is gone or not hand-made, its
# name still spells a number (207 digested it before the cut), or the
# name it digests is one the account no longer stores.
PRUNE_SQL = (
    "DELETE FROM account_sources s WHERE left(s.source_hmac, 2) = 'c:'"
    " AND NOT EXISTS (SELECT 1 FROM accounts a"
    "  WHERE a.tenant_id = s.tenant_id AND "
    + _C_ROW_TRUE_SQL.format(aid="s.account_id", row="s.source_hmac") + ")")

# Every household PRUNE_SQL or BACKFILL_SQL would change: one holding a
# c-row that is not true, or a hand-made account with a cut name and no
# c-row of it. `oikonome migrate` visits each under its sync lock
# (migrate.key_numbered_file_rows) and runs `backfill` there.
UNTRUE_TENANTS_SQL = (
    "SELECT s.tenant_id FROM account_sources s"
    " WHERE left(s.source_hmac, 2) = 'c:'"
    " AND NOT EXISTS (SELECT 1 FROM accounts a"
    "  WHERE a.tenant_id = s.tenant_id AND "
    + _C_ROW_TRUE_SQL.format(aid="s.account_id", row="s.source_hmac") + ")"
    " UNION SELECT a.tenant_id FROM accounts a WHERE " + _CUT_HAND_MADE_SQL
    + " AND NOT EXISTS (SELECT 1 FROM account_sources s"
    "  WHERE s.tenant_id = a.tenant_id AND s.door = 'manual'"
    "    AND s.account_id = a.id AND s.source_hmac = "
    + NAME_DIGEST_SQL.format(expr="a.name") + ")")


def hold_lock(conn) -> None:
    """The household's account-identity lock, for the rest of the caller's
    transaction: every door that decides which account a source gets — or
    mints one — and the re-key that moves ids take it, so two imports
    naming the same new source at once make one account."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 (_LOCK_KEY + str(conn.execute(
                     "SELECT current_setting('app.tenant_id', true) AS t"
                 ).fetchone()["t"]),))


def mint_id(conn, prefix: str = "") -> str:
    """A new opaque account id no account holds: letters only, so no
    pattern that looks for a number in an id ever reads one."""
    head = f"manual:{prefix}-" if prefix else "manual:"
    while True:
        aid = head + "".join(secrets.choice(_ID_LETTERS)
                             for _ in range(_ID_LEN))
        if conn.execute("SELECT 1 FROM accounts WHERE id = %s",
                        (aid,)).fetchone() is None:
            return aid


def _source_text(door: str, text: str) -> str:
    t = _WS_RE.sub(" ", str(text or "")).strip(" ")
    # a split file's source is compared folded (base._split_source); a
    # typed name keeps its case, as a hand-made account's reuse always has
    return t if door == DOOR_MANUAL else t.casefold()


def _name_digest(name: str) -> str:
    """The c-row of a name: over it with its whitespace folded and then
    every full number cut, so no digest of a whole number is ever
    written or asked for, however its groups are spaced."""
    t = card_number.cut(_WS_RE.sub(" ", str(name or ""))).strip(" ")
    return "c:" + hashlib.sha256(t.encode("utf-8")).hexdigest()


def stored_account_name(name):
    """The name a restored account stores for `name`: unchanged unless it
    spells a full number, which is cut (card_number.cut) after its
    whitespace is folded, so groups a tab or a line break separates are
    cut as groups a space separates are. A name whose slug still spells a
    number after that ("4000_0000_0000_0002", groups an en dash or a comma
    separates) is cut the way the slug reads it, as the new-account door
    stores it (card_number.cut_as_slug_reads). None stays None."""
    if name is None:
        return None
    from .base import NUMBERED_MANUAL_ID_RE
    folded = _WS_RE.sub(" ", str(name))
    shown = card_number.cut(folded)
    slug = re.sub(r"[^a-z0-9]+", "-", shown.lower()).strip("-")
    if NUMBERED_MANUAL_ID_RE.search(f"manual:{slug}"):
        shown = card_number.cut_as_slug_reads(shown)
    return name if shown == folded else shown


def c_row_holds(conn, digest: str, account_id: str) -> bool:
    """Whether a c-row `digest` naming `account_id` is true of the account
    as it is stored now (the rule PRUNE_SQL keeps by), so a door that
    lands one counts only a row the next prune keeps."""
    return conn.execute(
        "SELECT 1 FROM accounts a WHERE "
        + _C_ROW_TRUE_SQL.format(aid="%(aid)s", row="%(d)s"),
        {"aid": account_id, "d": digest,
         "p": _numbered_name_pattern()}).fetchone() is not None


def key_mark(secret: bytes) -> str:
    """Eight hex digits naming the secret a keyed row was written under:
    a row an archive brought from another household (whose secret never
    travels) is told from this household's own by it, so it is never read
    as this household's claim on an account."""
    return hmac.new(secret, b"account_sources", hashlib.sha256
                    ).hexdigest()[:8]


def _numbers(door: str, text: str) -> tuple[str, list[str]]:
    """(the source folded for its door, the digits of each full number in
    it)."""
    t = _source_text(door, text)
    return t, [re.sub(r"\D", "", n) for n in card_number.find(t)]


def _mac(secret: bytes, kind: str, door: str, body: str) -> str:
    return f"{kind}:{key_mark(secret)}:" + hmac.new(
        secret, f"{door}|{kind}|{body}".encode("utf-8"),
        hashlib.sha256).hexdigest()


def keyed(secret: bytes, door: str, text: str) -> str:
    """The s-row fingerprint of one source: "s:<key mark>:<hmac>"."""
    t, nums = _numbers(door, text)
    for spelled, digits in zip(card_number.find(t), nums):
        t = t.replace(spelled, digits)
    return _mac(secret, "s", door, t)


def card_key(secret: bytes, door: str, text: str) -> str | None:
    """The d-row fingerprint of the card(s) a source spells, or None when
    it spells no full number."""
    _, nums = _numbers(door, text)
    return _mac(secret, "d", door, ",".join(nums)) if nums else None


def _secret(conn, create: bool):
    from .base import txn_id_secret
    return txn_id_secret(conn, create=create)


def moved(conn, account_id: str) -> list[str]:
    """[account_id] while it is an account; else the live hand-made
    accounts a move took it to (a re-key off a full number, a reconnect's
    adoption), each followed to where it lives now
    (base.cut_lineage_targets) — [] when nothing did, several when cards
    sharing the last four left one record."""
    if conn.execute("SELECT 1 FROM accounts WHERE id = %s",
                    (account_id,)).fetchone():
        return [account_id]
    from .base import cut_lineage_targets
    found = cut_lineage_targets(conn, account_id)
    if not found:
        return []
    # only a live hand-made account is a place file rows may land: a
    # reconnect's adoption leads to a bank-feed account, and a hidden one
    # takes no rows
    return [r["id"] for r in conn.execute(
        "SELECT id FROM accounts WHERE id = ANY(%s) AND item_id = 'manual'"
        "   AND user_removed_at IS NULL ORDER BY id", (found,)).fetchall()]


def _live(conn, aid: str) -> str | None:
    """`aid`, or the one account a move took it to, when that is a live
    account; None otherwise."""
    found = moved(conn, aid)
    if len(found) != 1:
        return None
    row = conn.execute("SELECT user_removed_at FROM accounts WHERE id = %s",
                       (found[0],)).fetchone()
    return found[0] if row and row["user_removed_at"] is None else None


def lookup(conn, door: str, text: str) -> str | None:
    """The live account a source was given, or None. Reads only."""
    secret = _secret(conn, create=False)
    if secret is None:
        return None
    row = conn.execute(
        "SELECT account_id FROM account_sources"
        " WHERE door = %s AND source_hmac = %s",
        (door, keyed(secret, door, text))).fetchone()
    return _live(conn, row["account_id"]) if row else None


def claimed_accounts(conn, door: str, text: str) -> set[str]:
    """The accounts that already answer to a DIFFERENT full number at this
    door than the one `text` spells: for a source spelling a number, each
    is another card, however alike their cut names are. Empty for a source
    with no number; only rows written under this household's own secret
    count. Reads only."""
    secret = _secret(conn, create=False)
    mine = card_key(secret, door, text) if secret is not None else None
    if mine is None:
        return set()
    head = f"d:{key_mark(secret)}:"
    return {r["account_id"] for r in conn.execute(
        "SELECT DISTINCT account_id FROM account_sources WHERE door = %s"
        "   AND left(source_hmac, %s) = %s AND source_hmac <> %s",
        (door, len(head), head, mine)).fetchall()}


def claimed_by_another(conn, door: str, text: str, account_id: str) -> bool:
    """Whether `account_id` is one of claimed_accounts."""
    return account_id in claimed_accounts(conn, door, text)


def remember(conn, door: str, text: str, account_id: str, *,
             replace: bool = True) -> None:
    """Record that `text` at `door` fills `account_id`, and which card it
    spells. A later answer replaces an earlier one (a picker choice; a new
    account made because the one the row named is hidden or gone) unless
    `replace` is false."""
    secret = _secret(conn, create=True)
    conn.execute(
        """INSERT INTO account_sources (door, source_hmac, account_id)
           VALUES (%s, %s, %s)
           ON CONFLICT (tenant_id, door, source_hmac)
              WHERE left(source_hmac, 2) = 's:'
           DO UPDATE SET account_id = CASE WHEN %s
                         THEN EXCLUDED.account_id
                         ELSE account_sources.account_id END""",
        (door, keyed(secret, door, text), account_id, replace))
    card = card_key(secret, door, text)
    if card is not None:
        conn.execute(
            "INSERT INTO account_sources (door, source_hmac, account_id)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (door, card, account_id))


def remember_name(conn, account_id: str, name: str) -> None:
    """The c-row for a hand-made account's stored name."""
    if (name or "").strip():
        conn.execute(
            "INSERT INTO account_sources (door, source_hmac, account_id)"
            " VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (DOOR_MANUAL, _name_digest(name), account_id))


def by_name(conn, names) -> list[str]:
    """The live accounts a c-row of any of `names` leads to, in the order
    their rows were written."""
    digests = sorted({_name_digest(n) for n in names if (n or "").strip()})
    if not digests:
        return []
    out: list[str] = []
    for r in conn.execute(
            "SELECT s.account_id, s.source_hmac, a.name"
            "  FROM account_sources s JOIN accounts a ON a.id = s.account_id"
            " WHERE s.door = %s AND s.source_hmac = ANY(%s)"
            " ORDER BY s.created_at, s.account_id",
            (DOOR_MANUAL, digests)).fetchall():
        # a claim only while it is the digest of the name its account
        # stores: never a row an archive or a merge left naming another
        if _name_digest(r["name"]) != r["source_hmac"]:
            continue
        aid = _live(conn, r["account_id"])
        if aid and aid not in out:
            out.append(aid)
    return out


def _numbered_name_pattern() -> str:
    from .base import NUMBERED_NAME_PATTERN
    return NUMBERED_NAME_PATTERN


def backfill(conn) -> int:
    """Make the c-rows true: drop every one that is not the digest of its
    account's cut stored name (PRUNE_SQL), and write one for every
    hand-made account with a cut name that has none, as migration 207
    wrote them. Run after a restore lands an archive's accounts and after
    a cut rewrites a stored name; `oikonome migrate` runs it in every
    household it would change, under that household's sync lock. Returns
    the rows written."""
    args = {"p": _numbered_name_pattern()}
    conn.execute(PRUNE_SQL, args)
    return conn.execute(BACKFILL_SQL, args).rowcount
