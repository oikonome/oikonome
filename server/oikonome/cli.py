"""oikonome CLI: migrate + serve (dev) + reset-password (self-hosted)."""

import argparse
import os
import sys

from . import ext


def main() -> None:
    from . import logsafe
    logsafe.install()          # one line per record, as in the web process
    from . import ext
    p = argparse.ArgumentParser(prog="oikonome")
    sub = p.add_subparsers(dest="cmd", required=True)
    ext.cli_register(sub)
    sub.add_parser("migrate", help="apply schema + migrations")
    serve = sub.add_parser("serve", help="run the web app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8080)))
    serve.add_argument("--reload", action="store_true")
    rp = sub.add_parser("reset-password",
                        help="print a one-time password-reset link for a user "
                             "(self-hosted recovery — no email needed)")
    rp.add_argument("email")
    c2 = sub.add_parser("clear-2fa",
                        help="remove a user's authenticator, passkeys and "
                             "recovery codes and sign out every session — "
                             "for the person who has lost all three (verify "
                             "who is asking first)")
    c2.add_argument("email")
    sub.add_parser("keycheck",
                   help="verify stored secrets decrypt under the current "
                        "OIKONOME_MASTER_KEY (run after a restore)")
    sub.add_parser("reencrypt-secrets",
                   help="re-encrypt any tenant secrets left in plaintext "
                        "from a pre-master-key era (idempotent; the worker "
                        "also does this on startup)")
    sub.add_parser("rotate-master-key",
                   help="re-encrypt stored secrets from "
                        "OIKONOME_MASTER_KEY_OLD to the current "
                        "OIKONOME_MASTER_KEY (see docs/master-key.md)")
    iv = sub.add_parser("invite",
                        help="mint a signup invite for one email (prints "
                             "the signup link)")
    iv.add_argument("email", nargs="?", default=None)
    iv.add_argument("--days", type=int, default=14,
                    help="days until the invite expires (default 14)")
    iv.add_argument("--note", default=None,
                    help="operator memo stored with the invite")
    ds = sub.add_parser("demo-seed",
                        help="seed a FRESH instance with ~10 years of "
                             "synthetic household data")
    ds.add_argument("--seed", type=int, default=None,
                    help="RNG seed — same seed, same household")
    ds.add_argument("--years", type=int, default=10)
    ds.add_argument("--email", default="demo@example.com")
    ds.add_argument("--password", default=None)
    ds.add_argument("--viewer", default=None, metavar="EMAIL",
                    help="instead of a demo: a DEMONSTRATION household on "
                         "this (populated) instance — synthetic data, "
                         "demo_mode off, plus a view-only login at EMAIL "
                         "whose generated password is printed once")
    ak = sub.add_parser("admin-keys",
                        help="list or remove the admin console's operator "
                             "passkeys (break-glass — removing them "
                             "all makes OIKONOME_ADMIN_TOKEN work again, "
                             "with no restart)")
    ak.add_argument("--remove", metavar="ID",
                    help="remove ONE key by id (see the list)")
    ak.add_argument("--reset", action="store_true",
                    help="remove EVERY operator passkey — the lost-key "
                         "recovery. Sign in with the token afterwards and "
                         "enrol a new one.")
    ae = sub.add_parser("admin-enrol",
                        help="print a one-time link that enrols an admin "
                             "console passkey without signing in — the "
                             "recovery path for a passkey-only console")
    ae.add_argument("--minutes", type=int, default=15,
                    help="how long the link stays valid (default 15)")
    rr = sub.add_parser("demo-refresh",
                        help="regenerate a demonstration household's "
                             "synthetic data as of today, keeping both "
                             "logins and their enrolments")
    rr.add_argument("--email", required=True, metavar="EMAIL",
                    help="the view-only login naming the household")
    rr.add_argument("--years", type=int, default=10)
    rr.add_argument("--seed", type=int, default=None)

    dr = sub.add_parser("demo-reset",
                        help="wipe the demo household and regenerate it in "
                             "place (hourly pristine reset; same "
                             "seed + login unless overridden)")
    dr.add_argument("--seed", type=int, default=None,
                    help="override the stored demo_seed")
    dr.add_argument("--years", type=int, default=10)
    mr = sub.add_parser("merchants-resolve",
                        help="give every transaction its merchant row "
                             "(Plaid identity/logo, else the string clean) "
                             "for every tenant — the migration-097 backfill; "
                             "idempotent, safe to re-run")
    mr.add_argument("--tenant", default=None, help="one tenant id only")
    mr.add_argument("--reclean", action="store_true",
                    help="re-run the layer-1 string clean first, so a change "
                         "to the naming rules reaches names already in the "
                         "ledger (reviewed and hand-renamed names are left "
                         "alone)")
    ms = sub.add_parser("merchants-repair-splits",
                        help="converge merchants one payee was split "
                             "across when an aggregator enriched only some "
                             "of its rows; prints what it would merge, "
                             "--apply to merge (journalled, undoable)")
    ms.add_argument("--tenant", default=None, help="one tenant id only")
    ms.add_argument("--apply", action="store_true")
    ms.add_argument("--limit", type=int, default=None,
                    help="merges per tenant (default: the nightly's cap)")
    dt_ = sub.add_parser("dedupe-twins",
                         help="retire the second lineage of transactions that "
                              "reached an account twice under two ids (a "
                              "restore + reconnect seam); prints what it "
                              "would do, --apply to do it")
    dt_.add_argument("--tenant", required=True, help="tenant id")
    dt_.add_argument("--account", required=True,
                     help="the one account whose seam you have looked at — "
                          "never tenant-wide: repeated same-day charges in "
                          "ordinary history look exactly like twins")
    dt_.add_argument("--apply", action="store_true")
    args = p.parse_args()

    if ext.cli_dispatch(args):
        return
    if args.cmd == "dedupe-twins":
        raise SystemExit(dedupe_twins(args.tenant, args.account, args.apply))

    if args.cmd == "merchants-resolve":
        raise SystemExit(merchants_resolve(args.tenant, reclean=args.reclean))
    if args.cmd == "merchants-repair-splits":
        raise SystemExit(merchants_repair_splits(
            args.tenant, apply=args.apply, limit=args.limit))
    if args.cmd == "migrate":
        from .db import migrate
        names = migrate.run()
        print(f"migrations applied: {names or 'none (schema current)'}")
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("oikonome.web.app:app", host=args.host, port=args.port,
                    reload=args.reload)
    elif args.cmd == "reset-password":
        reset_password(args.email)
    elif args.cmd == "clear-2fa":
        clear_2fa(args.email)
    elif args.cmd == "reencrypt-secrets":
        from .db import migrate
        n = migrate.reencrypt_tenant_secrets()
        print(f"re-encrypted {n} plaintext tenant secret(s)")
    elif args.cmd == "keycheck":
        raise SystemExit(0 if keycheck() else 1)
    elif args.cmd == "rotate-master-key":
        raise SystemExit(0 if rotate_master_key() else 1)
    elif args.cmd == "invite":
        if args.email:
            mint_invite(args.email, args.days, args.note)
        else:
            p.error("invite needs an email")
    elif args.cmd == "admin-keys":
        raise SystemExit(admin_keys(remove=args.remove, reset=args.reset))
    elif args.cmd == "admin-enrol":
        raise SystemExit(admin_enrol(args.minutes))
    elif args.cmd == "demo-seed":
        import json

        from . import demo
        print(json.dumps(demo.seed(seed=args.seed, email=args.email,
                                   password=args.password,
                                   years=args.years,
                                   viewer=args.viewer)))
    elif args.cmd == "demo-refresh":
        from . import demo
        print(json.dumps(demo.refresh_demo_household(
            args.email, years=args.years, seed=args.seed)))
    elif args.cmd == "demo-reset":
        import json

        from . import demo
        r = demo.reset(seed_value=args.seed, years=args.years)
        print(json.dumps({k: v for k, v in r.items() if k != "password"}))


def merchants_resolve(tenant: str | None = None, *, reclean: bool = False) -> int:
    """Resolve merchant identity for every tenant's ledger and print the
    numbers (rows resolved, merchants, logos) — the rehearsal report.

    `reclean` re-runs the layer-1 string clean first (what the nightly pass
    does), so a change to the naming rules reaches strings that already have
    a canonical — the door to walk a fix onto live data instead of waiting
    for the night. Reviewed ('llm') and hand-renamed ('manual') names are
    untouched either way."""
    import json

    from .db import tenancy
    from .engine import merchant_dedup, merchant_identity
    admin = tenancy.admin_connect()
    try:
        rows = admin.execute("SELECT id FROM tenants" +
                             (" WHERE id=%s" if tenant else ""),
                             ((tenant,) if tenant else ())).fetchall()
    finally:
        admin.close()
    for r in rows:
        conn = tenancy.tenant_connect(str(r["id"]))
        try:
            st = {"recleaned": merchant_dedup.apply(conn)} if reclean else {}
            st |= merchant_identity.resolve(conn)
            st.update(merchant_identity.stats(conn))
            print(json.dumps({"tenant": str(r["id"]), **st}))
        finally:
            conn.close()
    return 0


def merchants_repair_splits(tenant: str | None = None, *, apply: bool = False,
                            limit: int | None = None) -> int:
    """Operator door onto the split-merchant repair — dry run unless --apply.

    One line per merge (or per proposal) and one per refusal, so the reason
    a descriptor was left alone is as visible as the merges: this sweep's
    job is to be conservative, and a refusal nobody can see reads as the
    sweep having done nothing."""
    import json

    from .db import tenancy
    from .engine import merchant_split_repair
    admin = tenancy.admin_connect()
    try:
        rows = admin.execute("SELECT id FROM tenants" +
                             (" WHERE id=%s" if tenant else ""),
                             ((tenant,) if tenant else ())).fetchall()
    finally:
        admin.close()
    for r in rows:
        tid = str(r["id"])
        conn = tenancy.tenant_connect(tid)
        try:
            kw = {"limit": limit} if limit is not None else {}
            got = merchant_split_repair.repair(conn, apply=apply, **kw)
            for rec in got["merges"]:
                print(json.dumps({"tenant": tid, "apply": apply, **rec},
                                 default=str))
            for rec in got["refused"]:
                print(json.dumps({"tenant": tid, "refused": True, **rec},
                                 default=str))
            print(json.dumps({"tenant": tid, "applied": got["applied"],
                              "split_descriptors": got["descriptors"],
                              "merged": len(got["merges"]),
                              "refused": len(got["refused"]),
                              "deferred": got["deferred"]}))
        finally:
            conn.close()
    return 0


def dedupe_twins(tenant: str, account: str, apply: bool) -> int:
    """Operator door onto sync.adopt.dedupe_twins — ONE account, dry-run
    unless --apply."""
    import json
    from .db import tenancy
    from .sync import adopt
    conn = tenancy.tenant_connect(tenant)
    try:
        ids = [account]
        total = 0
        for aid in ids:
            for rec in adopt.dedupe_twins(conn, aid, apply=apply):
                total += 1
                print(json.dumps({"apply": apply, **rec}, default=str))
        # tenant connections are autocommit — every retire is already durable
        print(json.dumps({"tenant": tenant, "accounts": len(ids), "twins": total,
                          "applied": apply}))
    finally:
        conn.close()
    return 0


def admin_enrol(minutes: int = 15) -> int:
    """Mint a one-time admin-console enrolment link.

    This is the whole recovery story for a console with NO operator token.
    Removing credentials cannot be the answer there — it recovers to
    nothing — so the answer is a link that enrols a NEW key without any
    credential, mintable only from inside the container, i.e. only by
    someone who already controls the host. Single use, short-lived, stored
    hashed, and it buys exactly one key.
    """
    from .auth import admin_passkeys
    from .db import tenancy
    admin = tenancy.admin_connect()
    try:
        ticket = admin_passkeys.mint_enrol_ticket(admin, minutes)
        admin.execute(
            "INSERT INTO admin_audit (action, target, ip, actor) "
            "VALUES ('admin_enrol_link', %s, 'host-cli', 'host-cli')",
            (f"valid {minutes} min",))
        n = admin_passkeys.count(admin)
    finally:
        admin.close()
    # One link per configured origin. Printing only OIKONOME_BASE_URL is a
    # trap: a console reachable on a private network is opened at a name the
    # public URL cannot resolve, so the single printed link would be the one
    # link that cannot be opened on the device that needs it.
    from .web.adminconsole import _console_origins
    origins = _console_origins()
    print(f"Enrol a console passkey (valid {minutes} min, single use — ONE "
          "key, at ONE origin):\n")
    for o in origins:
        print(f"  {o}/admin/console/enrol?t={ticket}")
    if not origins:
        print(f"  /admin/console/enrol?t={ticket}")
        print("\n! Neither OIKONOME_BASE_URL nor OIKONOME_ADMIN_ORIGINS is "
              "set, so the host part above is missing — prefix it with your "
              "console's URL.")
    elif len(origins) > 1:
        # WebAuthn binds the credential to the origin it was created at, so
        # the choice of link is the choice of which sign-in page the key
        # will work on. Say so — it is not obvious and it is not recoverable
        # without minting another link.
        print("\nOpen the one you will SIGN IN at: the key is bound to that "
              "origin and will not be offered at the others.")
    print()
    if n:
        print(f"Note: {n} key(s) are still enrolled. If you are recovering "
              "from a LOST key, remove them after you can sign in again:")
        print("  oikonome admin-keys --remove <id>")
    return 0


def admin_keys(remove: str | None = None, reset: bool = False) -> int:
    """Break-glass for the admin console's operator passkeys, from the host.

    This is the answer to "I lost my key" for an operator who keeps exactly
    ONE. Removing every credential restores the OIKONOME_ADMIN_TOKEN path
    immediately — `OIKONOME_ADMIN_REQUIRE_PASSKEY` is ignored while none are
    enrolled, by design — so recovery is one command with **no .env edit and
    no restart**.

    The authorization boundary is deliberate and unchanged: you must be able
    to run a command inside the container, i.e. you already own the host. A
    stolen console session cannot reach this, and the console's own delete
    still demands a fresh assertion.
    """
    from .auth import admin_passkeys
    from .db import tenancy
    admin = tenancy.admin_connect()
    try:
        if remove:
            # A credential-only DELETE leaves sessions live (FK SET NULL);
            # admin_passkeys.delete kills sessions first.
            try:
                ok = admin_passkeys.delete(admin, remove)
            except Exception:                                # noqa: BLE001
                print(f"not a valid key id: {remove}", file=sys.stderr)
                return 1
            if not ok:
                print(f"no operator passkey with id {remove}",
                      file=sys.stderr)
                return 1
            admin.execute(
                "INSERT INTO admin_audit (action, target, ip, actor) "
                "VALUES ('admin_passkey_removed', %s, 'host-cli', 'host-cli')",
                (remove,))
            print(f"removed operator passkey {remove}")
        elif reset:
            n = admin_passkeys.reset_all(admin)
            admin.execute(
                "INSERT INTO admin_audit (action, target, ip, actor) "
                "VALUES ('admin_passkey_reset', %s, 'host-cli', 'host-cli')",
                (f"{n} removed",))
            print(f"removed {n} operator passkey(s).")
            print("The operator token signs in again — no restart needed, "
                  "even with OIKONOME_ADMIN_REQUIRE_PASSKEY set.")
            print("Sign in with it and enrol a new key.")
        rows = admin.execute(
            "SELECT id, label, created_at, last_used FROM admin_credentials "
            "ORDER BY created_at").fetchall()
    finally:
        admin.close()
    if not rows:
        print("no operator passkeys enrolled — the console is token-only")
        return 0
    print(f"{len(rows)} operator passkey(s):")
    for r in rows:
        used = r["last_used"].strftime("%Y-%m-%d %H:%M") if r["last_used"] \
            else "never"
        print(f"  {r['id']}  {r['label'] or '(unnamed)':<24} "
              f"added {r['created_at']:%Y-%m-%d}  last used {used}")
    return 0


# Every control-plane column that holds a ciphertext written by
# crypto.encrypt_cp — i.e. encrypted DIRECTLY under the master key, with no
# per-tenant data key in between. keycheck probes each and rotate-master-key
# re-wraps each; a CP secret missing from this list survives a rotation
# unreadable, and keycheck would still say the old key can be dropped.
# (table, primary-key column, ciphertext column)
CP_CIPHERTEXT_COLUMNS = (
    ("users", "id", "totp_secret"),            # per-user TOTP seed
    ("push_vapid", "id", "private_key"),       # the instance web-push key
)


def _cp_ciphertexts(admin, lock: bool = False) -> list[tuple]:
    """(table, pk_col, col, pk, ciphertext) for every stored CP secret."""
    from .db import crypto
    rows = []
    for table, pk, col in CP_CIPHERTEXT_COLUMNS:
        for r in admin.execute(
                f"SELECT {pk} AS pk, {col} AS ct FROM {table} "
                f"WHERE {col} LIKE %s" + (" FOR UPDATE" if lock else ""),
                (crypto.CP_PREFIX + "%",)).fetchall():
            rows.append((table, pk, col, r["pk"], r["ct"]))
    return rows


def keycheck() -> bool:
    """Master-key continuity probe: do the secrets stored in THIS
    database decrypt under the CURRENT OIKONOME_MASTER_KEY? A dump restored
    from another install loads fine, but its ciphertexts are unreadable
    until the original key is set — surface that now, not as crypto errors
    mid-sync. Probes every wrapped tenant data key (each tenant with any
    encrypted secret has one) plus every control-plane ciphertext
    (CP_CIPHERTEXT_COLUMNS: TOTP seeds, the web-push signing key), which
    encrypt directly under the master key."""
    from cryptography.fernet import Fernet, InvalidToken

    from .db import crypto, tenancy

    admin = tenancy.admin_connect()   # RLS bypass: keys of EVERY tenant
    try:
        wrapped = [r["wrapped_key"] for r in admin.execute(
            "SELECT wrapped_key FROM tenant_keys").fetchall()]
        totp = [row[4] for row in _cp_ciphertexts(admin)]
    finally:
        admin.close()
    if not wrapped and not totp:
        print("ok: no encrypted secrets stored")
        return True
    mk = crypto.master_key()
    if not mk:
        print("MISMATCH: encrypted secrets exist but OIKONOME_MASTER_KEY "
              "is not set", file=sys.stderr)
        return False
    fernet = Fernet(mk)
    bad = 0
    for token in wrapped:
        try:
            fernet.decrypt(token.encode())
        except (InvalidToken, ValueError):
            bad += 1
    for token in totp:
        try:
            fernet.decrypt(token[len(crypto.CP_PREFIX):].encode())
        except (InvalidToken, ValueError):
            bad += 1
    if bad:
        print(f"MISMATCH: {bad} of {len(wrapped) + len(totp)} stored "
              "secrets do NOT decrypt under the current OIKONOME_MASTER_KEY",
              file=sys.stderr)
        return False
    print(f"ok: {len(wrapped) + len(totp)} stored secret(s) decrypt under "
          "the current master key")
    return True


def rotate_master_key() -> bool:
    """Rotate the master key. The envelope design makes this
    cheap — tenant secrets encrypt under per-tenant DATA keys, so rotation
    never touches secret rows; it re-wraps each tenant_keys row (old
    master → new) plus every control-plane ciphertext (CP_CIPHERTEXT_COLUMNS
    — TOTP seeds and the web-push signing key), which encrypt directly
    under the master. Run with the NEW key in OIKONOME_MASTER_KEY
    and the OLD one in OIKONOME_MASTER_KEY_OLD (procedure incl. the
    container invocation: docs/master-key.md).

    Idempotent: a row that already decrypts under the new key is skipped,
    so an interrupted rotation can simply be re-run."""
    from cryptography.fernet import Fernet, InvalidToken

    from .db import crypto, tenancy

    new = crypto.master_key()
    old = os.environ.get("OIKONOME_MASTER_KEY_OLD", "")
    if not new or not old:
        print("set OIKONOME_MASTER_KEY (new) and OIKONOME_MASTER_KEY_OLD "
              "(old) before rotating", file=sys.stderr)
        return False
    f_new, f_old = Fernet(new), Fernet(old.encode())

    def reissue(token: bytes) -> bytes | None:
        """old-ciphertext → new-ciphertext; None = already rotated."""
        try:
            plain = f_old.decrypt(token)
        except (InvalidToken, ValueError):
            f_new.decrypt(token)          # neither key? raise, roll back
            return None
        return f_new.encrypt(plain)

    admin = tenancy.admin_connect()   # RLS bypass: keys of EVERY tenant
    rewrapped = skipped = 0
    try:
        with admin.transaction():
            for r in admin.execute(
                    "SELECT tenant_id, wrapped_key FROM tenant_keys "
                    "FOR UPDATE").fetchall():
                out = reissue(r["wrapped_key"].encode())
                if out is None:
                    skipped += 1
                    continue
                admin.execute(
                    "UPDATE tenant_keys SET wrapped_key=%s "
                    "WHERE tenant_id=%s", (out.decode(), r["tenant_id"]))
                rewrapped += 1
            for table, pk, col, pk_val, ct in _cp_ciphertexts(admin,
                                                              lock=True):
                out = reissue(ct[len(crypto.CP_PREFIX):].encode())
                if out is None:
                    skipped += 1
                    continue
                admin.execute(
                    f"UPDATE {table} SET {col}=%s WHERE {pk}=%s",
                    (crypto.CP_PREFIX + out.decode(), pk_val))
                rewrapped += 1
    except (InvalidToken, ValueError):
        print("MISMATCH: a stored secret decrypts under NEITHER key — "
              "wrong OIKONOME_MASTER_KEY_OLD? Nothing was changed.",
              file=sys.stderr)
        return False
    finally:
        admin.close()
    print(f"rotated: {rewrapped} secret(s) re-encrypted"
          + (f", {skipped} already current" if skipped else ""))
    print("keep the old key until a keycheck against your latest BACKUP "
          "passes with the new one (docs/master-key.md)", file=sys.stderr)
    return True


def mint_invite(email: str, days: int, note: str | None) -> None:
    """Mint a signup invite (admin role — the app role deliberately cannot
    INSERT into signup_invites) and print the link."""
    from urllib.parse import quote

    from .auth import signup_invites
    from .db import tenancy

    admin = tenancy.admin_connect()
    try:
        token = signup_invites.mint(admin, email, days=days, note=note)
        # a pending access request for this address, if an add-on keeps
        # them, is now answered
        ext.gate.on_invite_minted(admin, email.strip().lower())
    finally:
        admin.close()
    path = f"/signup?invite={token}&email={quote(email.strip().lower())}"
    base = os.environ.get("OIKONOME_BASE_URL", "").rstrip("/")
    print(f"{base}{path}" if base else path)
    # the LIFETIME MINTED, not the number typed: mint clamps to 1..3650, so
    # echoing the typed number would report `--days 999999` as 999999 on a
    # 3650-day invite, and `--days 0` as "already dead" on a one-day one
    print(f"(single use, expires in {signup_invites.bounded_days(days)} "
          f"days, only valid for {email.strip().lower()})", file=sys.stderr)


def reset_password(email: str) -> None:
    """Mint a reset token for the user and print the link path. Same token
    machinery as /forgot: 1-hour TTL, one use, revokes all sessions on use."""
    import psycopg
    from psycopg.rows import dict_row

    from .auth import reset as reset_mod
    from .db import tenancy

    with psycopg.connect(tenancy.APP_DSN, row_factory=dict_row,
                         autocommit=True) as conn:
        u = conn.execute("SELECT id FROM users WHERE email=%s",
                         (email.strip().lower(),)).fetchone()
        if u is None:
            print(f"no user with email {email!r}", file=sys.stderr)
            raise SystemExit(1)
        token = reset_mod.create_reset(conn, u["id"])
    print(f"/reset?token={token}")
    print("(valid 1 hour, one use — open it on your instance's URL)",
          file=sys.stderr)


def clear_2fa(email: str) -> None:
    """The operator door for a user who has lost password, authenticator
    and recovery codes together: void TOTP, passkeys and recovery codes,
    evict every session and device (exactly what a reset does), leave the
    password alone, and tell the account by mail. Print the reset link
    next so the operator can hand it over in the same breath."""
    from .auth import factor_reset
    from .db import tenancy

    admin = tenancy.admin_connect()
    try:
        u = admin.execute("SELECT id FROM users WHERE email=%s",
                          (email.strip().lower(),)).fetchone()
        if u is None:
            print(f"no user with email {email!r}", file=sys.stderr)
            raise SystemExit(1)
        removed = factor_reset.clear_second_factor(admin, u["id"])
        admin.commit()
        admin.execute(
            "INSERT INTO admin_audit (action, target, detail, ip, actor) "
            "VALUES ('clear_second_factor', %s, to_jsonb(%s::text), NULL, "
            "'cli')",
            (email.strip().lower(),
             f"totp={'yes' if removed['totp'] else 'no'} "
             f"passkeys={removed['passkeys']} "
             f"sessions_evicted={removed['sessions']}"))
        admin.commit()
    finally:
        admin.close()
    from .web.app import _deliver_factor_cleared_notice
    _deliver_factor_cleared_notice(email.strip().lower())
    print(f"second factor cleared for {email}: "
          f"TOTP {'removed' if removed['totp'] else 'was off'}, "
          f"{removed['passkeys']} passkey(s) and all recovery codes removed, "
          f"{removed['sessions']} session(s) signed out; the account was "
          f"emailed.", file=sys.stderr)
    print("Set a new password with:  oikonome reset-password "
          f"{email}", file=sys.stderr)


# `oikonome …` (the console script in pyproject.toml) is the documented way in,
# and oikonome.sh uses it. But docs/master-key.md spells the master-key
# procedure `python -m oikonome.cli keycheck` / `rotate-master-key`, and
# without this guard that form imports the module, defines main(), calls
# nothing, and exits 0 — silently. An operator following the documented
# rotation would see a clean exit, believe the key had rotated, and be free to
# destroy the old one. Both spellings must actually run.
if __name__ == "__main__":
    main()
