"""One-time recovery codes — the lockout safety net for forced 2FA.

A hosted account must enroll a second factor (TOTP or passkey). A TOTP user
who loses their authenticator would otherwise be locked out, so enrollment
issues a batch of one-time recovery codes (shown once). Codes are sha256 at
rest (like every other token here) and single-use.

Format: 5 groups of 4 lowercase base32-ish chars, dash-separated
(e.g. `k7f2-9qtp-m3xz-...`) — easy to write down, hard to guess.
"""

from __future__ import annotations

import hashlib
import secrets

_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no ambiguous 0/o/1/i/l
BATCH = 8


def _fmt() -> str:
    groups = ["".join(secrets.choice(_ALPHABET) for _ in range(4))
              for _ in range(5)]
    return "-".join(groups)


def _h(code: str) -> str:
    # normalize: strip spaces + lowercase so "AB CD" matches "abcd"
    return hashlib.sha256(
        code.strip().lower().replace(" ", "").encode()).hexdigest()


def lock_codes(conn, user_id) -> None:
    """The per-user lock every writer of the code table takes first, held
    to the end of its transaction. A staged batch commits after the door's
    factor lock is gone, so without this a second reissue (or a reset's
    drop) running in that gap would sweep only the rows it could see and
    leave the staged batch standing beside its own. Under it, the later
    writer waits for the earlier commit and then sees that batch.

    Lock order: the factor lock (control connection) first, then this one
    (admin connection). A holder of this lock never asks for the factor
    lock, so the two cannot wait on each other. A different key from the
    factor lock on purpose: the door holds that one on its control
    connection, and taking it again from the admin connection would wait
    on itself."""
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                 ("oikonome:recovery-codes:" + str(user_id),))


def issue(admin_conn, user_id, n: int = BATCH) -> list[str]:
    """Replace any prior codes and mint a fresh batch. Returns the PLAINTEXT
    codes (shown once); only their hashes persist. Admin connection — the
    app role can't INSERT here (mint is a server-side act).

    The void and the new batch commit together: the admin connection is
    autocommit, so statement by statement the old sheet would be gone the
    moment the DELETE ran, and a failure before the inserts would leave
    the owner with no codes and none shown."""
    codes = [_fmt() for _ in range(n)]
    with admin_conn.transaction():
        lock_codes(admin_conn, user_id)
        admin_conn.execute("DELETE FROM recovery_codes WHERE user_id=%s",
                           (user_id,))
        for c in codes:
            admin_conn.execute(
                "INSERT INTO recovery_codes (user_id, code_hash) "
                "VALUES (%s, %s)", (user_id, _h(c)))
    return codes


def hold(admin_conn, user_id, tenant_id=None) -> bool:
    """Take the code lock for a batch about to be minted, then wait out
    any writer holding the account's users row and re-read which
    household the account is in. False when the account is gone or now
    belongs to another household than `tenant_id`.

    The reclaim's member road moves an account into a fresh household
    under `users ... FOR UPDATE` and deletes its codes there, without the
    code lock (a lock taken under that row lock would deadlock against a
    staged batch's foreign-key share lock). A reissue that started before
    the move commits would otherwise land its batch on the moved account,
    for whoever held the old session. FOR KEY SHARE waits for that row
    lock, then reads the committed household, so the reissue stops
    instead. Taken before the door writes anything else, so the wait
    never holds a row the move is queued behind."""
    lock_codes(admin_conn, user_id)
    row = admin_conn.execute(
        "SELECT tenant_id FROM users WHERE id=%s FOR KEY SHARE",
        (user_id,)).fetchone()
    return row is not None and (tenant_id is None
                                or str(row["tenant_id"]) == str(tenant_id))


def drop(admin_conn, user_id) -> None:
    """Void the account's sheet, minting nothing, under the code lock."""
    with admin_conn.transaction():
        lock_codes(admin_conn, user_id)
        admin_conn.execute("DELETE FROM recovery_codes WHERE user_id=%s",
                           (user_id,))


class issue_here:
    """The staged batch's interface on the door's own connection, for a
    door whose whole change already runs on an admin connection (the
    login-email doors). One transaction, so the batch commits with the
    change; and one connection, so the door's own users-row write never
    waits on a lock its batch holds."""

    def __init__(self, admin_conn):
        self._conn = admin_conn

    def hold(self, user_id, tenant_id=None) -> bool:
        return hold(self._conn, user_id, tenant_id)

    def issue(self, user_id, n: int = BATCH) -> list[str]:
        return issue(self._conn, user_id, n)

    def drop(self, user_id) -> None:
        drop(self._conn, user_id)


class issue_staged:
    """A batch minted inside a door's own transaction and committed only
    after it, for the doors that void the old sheet as part of a change
    made on the control connection (a factor removed, sessions swept).

    The batch has to go in on an admin connection, so it cannot share the
    door's transaction. Committed first, a failure in the door's own
    commit would leave the old sheet void and the new one never shown;
    committed here, after the door's transaction has committed, a failure
    leaves the old sheet standing. Until then the old rows are held by the
    open DELETE, so a redeem or a sign-in re-check that touches them waits
    and then finds them gone, never reads them as still live.

        with recovery.issue_staged(tenancy.admin_connect, open_now) as b:
            with control.transaction():
                ...
                codes = b.issue(user_id)

    `open_now` connects on entry — before the door takes its factor lock,
    so the critical section never waits on a connect; otherwise `issue`
    connects on first use."""

    def __init__(self, connect, open_now: bool = True):
        self._connect = connect
        self._open_now = open_now
        self._conn = None
        self._tx = None

    def _open(self):
        if self._conn is None:
            self._conn = self._connect()
            self._tx = self._conn.transaction()
            self._tx.__enter__()

    def __enter__(self):
        if self._open_now:
            try:
                self._open()
            except BaseException:
                self._close()
                raise
        return self

    def issue(self, user_id, n: int = BATCH) -> list[str]:
        self._open()
        return issue(self._conn, user_id, n)

    def hold(self, user_id, tenant_id=None) -> bool:
        """See `hold`: the code lock and the household re-check, held to
        this batch's commit."""
        self._open()
        return hold(self._conn, user_id, tenant_id)

    def drop(self, user_id) -> None:
        """Void the sheet with nothing minted, committed after the door."""
        self._open()
        drop(self._conn, user_id)

    def _close(self):
        if self._conn is not None:
            self._conn.close()

    def __exit__(self, et, ev, tb):
        try:
            if self._tx is not None:
                self._tx.__exit__(et, ev, tb)
        finally:
            self._close()
        return False


def redeem(conn, user_id, code: str) -> bool:
    """Consume a recovery code for this user. True iff it matched an unused
    code (which is now burned). Constant-ish work: always hashes."""
    return redeem_id(conn, user_id, code) is not None


def redeem_id(conn, user_id, code: str):
    """`redeem`, answering with the burned row's id (None on a miss), so a
    sign-in can ask later whether the sheet that code came from still
    stands (see still_issued)."""
    if not code or len(code.strip()) < 8:
        return None
    row = conn.execute(
        "UPDATE recovery_codes SET used_at=now() "
        "WHERE user_id=%s AND code_hash=%s AND used_at IS NULL "
        "RETURNING id", (user_id, _h(code))).fetchone()
    return row["id"] if row else None


def still_issued(conn, user_id, code_id) -> bool:
    """Whether a redeemed code's batch is still the account's. A reissue
    deletes every row, used ones included, so a code spent from a sheet
    that has since been voided is gone. FOR SHARE so a reissue still in
    flight (its DELETE not yet committed) is waited for, not read past."""
    return conn.execute(
        "SELECT 1 FROM recovery_codes WHERE id=%s AND user_id=%s FOR SHARE",
        (code_id, user_id)).fetchone() is not None


def remaining(conn, user_id) -> int:
    return conn.execute(
        "SELECT count(*) n FROM recovery_codes "
        "WHERE user_id=%s AND used_at IS NULL", (user_id,)).fetchone()["n"]
