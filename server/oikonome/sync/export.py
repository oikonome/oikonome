"""The full-data export — one ZIP of CSVs, the user's whole household as
plain files. Built here so the download door (web/pages.export_data) and
the operator/test clone path (restore this ZIP into a throwaway tenant)
produce byte-identical archives; the README inside names every file."""
from __future__ import annotations

import csv as _c
import io as _io
import zipfile

from .. import ext


DATA_MAP = {
    "items": "bank/brokerage connections (one row per institution login)",
    "accounts": "your accounts — names, types, balances",
    "transactions": "every transaction (amount positive = money out)",
    "bills": "recurring bills and income the app tracks",
    "bill_proposals": "detected-but-unconfirmed recurring candidates",
    "reimbursements": "expense↔repayment pairs you linked",
    "reimburse_flags": "transactions marked awaiting reimbursement",
    "business_flags": "transactions flagged business on personal cards",
    "manual_categories": "per-transaction category overrides",
    "merchants": "merchants as rows — name, Plaid entity id, logo, "
                 "website, kind; transactions.merchant_id points here",
    "alerts_log": "alert history",
    "import_batches": "file-import history (what was imported, when)",
    "liabilities": "credit-card / loan terms (due dates, APRs)",
    "holdings": "investment holdings (positions and prices)",
    "crypto_holdings": "crypto positions",
    "networth_recorded": "net-worth history (recorded points)",
    "networth_snapshot": "net-worth history (nightly snapshots)",
    "income_annual": "yearly income summary",
    "income_documents": "income documents metadata (W-2s and the like)",
    "merchant_canonical": "merchant-name spelling variants → one name",
    "merchant_renames": "your merchant rename/merge history (the undo "
                        "journal behind the Merchants screen)",
    "merchant_categories": "learned merchant→category rules + source",
    "merchant_merge_proposals": "merchants the app offered to merge as one "
                                "business, and what you said about each",
    "amazon_orders": "imported Amazon order history",
    "amazon_matches": "Amazon orders matched to card charges",
    "amazon_summaries": "short item summaries for matched orders",
    "costco_receipts": "imported Costco receipts with their line items",
    "costco_matches": "Costco receipts matched to card charges",
    "business_entity": "your business entities",
    "entity_membership": "who belongs to which business",
    "equity_movement": "owner draws / contributions",
    "business_txn_class": "business transaction classifications",
    "compliance_obligation": "business compliance calendar entries",
    "mileage_log": "business mileage entries",
    "vendor_1099": "1099 vendor records",
    "transaction_notes": "your notes on transactions",
    "account_links": "same-account links across data sources",
    "receipts": "receipt images (base64 in the CSV)",
    "receipt_items": "receipt line items",
    "tenant_settings": "your settings (credentials scrubbed)",
    "budget_snapshots": "each closed month's budget, as it stood then",
    "recipient_invites": "everyone you added to the daily email — the "
                         "address, who invited them, and whether they said "
                         "yes (the invite link itself never travels)",
}


# Tables with a tenant_id and NO row-level security are invisible to the
# discovery below — it asks Postgres which tables have RLS on, precisely so
# that a plain SELECT can never return another household's rows. That makes
# them a hand-maintained list, and a control-plane table left off it goes
# missing from this ZIP without anything failing. `recipient_invites` is the
# example that has to be named: the daily email's recipients and their
# consent stamps live in a table no door can see, so a migration would carry
# the recipient LIST across (it is ordinary config) and leave the acceptances
# behind — and the worker mails an extra recipient only if they accepted, so
# the addresses reappear in Settings while the mail silently stops.
#
# So both halves are named, and `test_control_plane_export_coverage` fails
# until a newly added control-plane table joins one of them.
CONTROL_TABLES = ("recipient_invites",)

CONTROL_SKIP = {
    "users": "the household roster. Accounts are created by signing up or "
             "accepting an invite; a data file must not be able to mint one.",
    "sessions": "live logins on the instance the ZIP came from.",
    "invites": "pending household invites, keyed to tokens in somebody's "
               "mailbox that point at the source instance.",
    "api_tokens": "collector credentials — re-issued on the destination.",
    "device_tokens": "phone enrolments, bound to the source instance.",
    "push_subscriptions": "browser push endpoints, bound to the source "
                          "instance's keys.",
    "support_consents": "consent to let THIS platform's operator read the "
                        "data — it cannot be transferred to another one.",
} | ext.gate.export_exclusions()

def _control_csv(conn, table: str) -> str:
    """One control-plane table as a CSV, scoped to the tenant `conn` is
    bound to.

    Two things differ from every other member. The rows come off the ADMIN
    connection, because the migration that created `recipient_invites` grants
    the app role nothing at all — an `accepted_at` row is what makes a
    household's financial mail flow to a mailbox, so forging one has to stay
    out of reach of injected app-role SQL. The tenant therefore has to be
    named explicitly in the WHERE clause rather than left to RLS, which is
    also why this stays a small hand-written query per table instead of a
    generic SELECT *.

    And the token hash never travels. It is a credential in two directions:
    the raw token is sitting in a recipient's mailbox, so a hash that rode
    the ZIP would let that same link enrol them on any instance the archive
    was restored into — an invite answered on a copy the recipient has never
    heard of. `invited_by` travels as the inviter's ADDRESS, not their user
    id: the ZIP carries no users, so the id would point at nothing (and at
    the wrong person on a destination that happens to reuse it).
    """
    if table != "recipient_invites":                # pragma: no cover
        raise ValueError(f"no control-plane export defined for {table}")
    from ..db import tenancy
    from ..web.pages import _csv_cell
    row = conn.execute(
        "SELECT current_setting('app.tenant_id', true) AS tid").fetchone()
    tid = row["tid"] if row else None
    cols = ["email", "invited_by_email", "created_at", "expires_at",
            "accepted_at", "declined_at", "last_sent_at", "send_count"]
    rows = []
    if tid:
        admin = tenancy.admin_connect()
        try:
            rows = admin.execute(
                """SELECT i.email, u.email AS invited_by_email, i.created_at,
                          i.expires_at, i.accepted_at, i.declined_at,
                          i.last_sent_at, i.send_count
                     FROM recipient_invites i
                     LEFT JOIN users u ON u.id = i.invited_by
                    WHERE i.tenant_id = %s
                    ORDER BY i.email""", (tid,)).fetchall()
        finally:
            admin.close()
    sio = _io.StringIO()
    w = _c.DictWriter(sio, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow({k: _csv_cell(r[k]) for k in cols})
    return sio.getvalue()


def build_zip(conn, out=None) -> bytes | None:
    """The export for the tenant `conn` is scoped to. Credentials never
    travel (tenant_settings is scrubbed; the secret columns are excluded);
    receipt blobs DO — this is the user's full copy."""
    from ..web.pages import EXPORT_SKIP, _EXPORT_ORDER, _csv_cell
    # The same RLS-only discovery the portable metadata archive
    # (tenant_export) uses:
    # tenant_id alone is NOT enough (users/invites/api_tokens carry it
    # with NO row-level security — an unscoped SELECT there would be
    # every tenant's rows), so membership is "RLS actually enabled",
    # minus the conscious EXPORT_SKIP set above
    from ..tenant_export import rls_tenant_tables
    from .restore import RESTORED_MEMBERS as _RESTORED
    discovered = [t for t in rls_tenant_tables(conn)
                  if t not in EXPORT_SKIP]
    TABLES = ([t for t in _EXPORT_ORDER if t in discovered]
              + [t for t in discovered if t not in _EXPORT_ORDER]
              # the hand-named control-plane members, last: discovery
              # cannot see them (see CONTROL_TABLES above)
              + list(CONTROL_TABLES))
    readme = (
        "Oikonome full data export\n"
        "=========================\n\n"
        "Every file is a plain CSV — open any of them in a spreadsheet.\n"
        "Amounts follow the ledger convention: POSITIVE = money out.\n"
        "This ZIP restores into any Oikonome instance (Settings -> Your\n"
        "data on self-host or hosted); credentials never travel in it, so\n"
        "bank connections are re-linked on the destination.\n\n"
        "What each file is. Files marked (records only) are written for\n"
        "your own reading and are NOT loaded by a restore — they hold\n"
        "this instance's operating state (alerts, sync logs, staged\n"
        "uploads), which the destination rebuilds for itself.\n\n"
        + "\n".join(
            f"  {t}.csv — {DATA_MAP.get(t, '')}"
            + ("" if t in _RESTORED else "  (records only)")
            for t in TABLES)
        + "\n")
    from ..tenant_export import _EXCLUDE_COLS
    # The metadata archive's secret-column set, plus tenant_id (ambient
    # here, meaningless to the recipient) — membership is discovered, so
    # the column filter must be shared too, or a future secret column
    # rides out in a table nobody hand-vetted. Blob columns are the one
    # deliberate difference: this ZIP is the user's full copy, so receipt
    # images DO travel here, while the metadata archive omits them by
    # design.
    _blobs = {"raw_bytes", "image", "image_optimized", "content"}
    drop = (_EXCLUDE_COLS - _blobs) | {"tenant_id"}
    buf = out if out is not None else _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("README.txt", readme)
        for tbl in TABLES:
            if tbl in CONTROL_TABLES:
                z.writestr(f"{tbl}.csv", _control_csv(conn, tbl))
                continue
            if tbl == "tenant_settings":
                # one row, and it needs the deep credential scrub — the
                # only table still materialized before writing
                import json as _j

                from ..sync.restore import scrub_config as _scrub
                rows = []
                for r in conn.execute("SELECT * FROM tenant_settings"
                                      ).fetchall():
                    cfg = r["config"]
                    cfg = cfg if isinstance(cfg, dict) else _j.loads(cfg)
                    d = dict(r)
                    d["config"] = _j.dumps(_scrub(cfg))
                    rows.append(d)
                sio = _io.StringIO()
                if rows:
                    cols = [c for c in rows[0].keys() if c not in drop]
                    w = _c.DictWriter(sio, fieldnames=cols,
                                      extrasaction="ignore")
                    w.writeheader()
                    for r in rows:
                        w.writerow({k: _csv_cell(r[k]) for k in cols})
                z.writestr(f"{tbl}.csv", sio.getvalue())
                continue
            # Every other table STREAMS: a server-side cursor hands rows
            # over one batch at a time and each is written straight into
            # the zip member. A fetchall() per table — receipts included,
            # whose image column is base64 photos — would take a household
            # with a year of receipts past the process memory limit, on
            # the one door out.
            with z.open(f"{tbl}.csv", "w") as raw:
                fh = _io.TextIOWrapper(raw, encoding="utf-8",
                                       newline="", write_through=True)
                w = None
                with conn.transaction():
                    with conn.cursor(name=f"exp_{tbl}") as cur:
                        cur.itersize = 500
                        cur.execute(f"SELECT * FROM {tbl}")
                        for r in cur:
                            r = dict(r)
                            if w is None:
                                cols = [c for c in r.keys()
                                        if c not in drop]
                                w = _c.DictWriter(fh, fieldnames=cols,
                                                  extrasaction="ignore")
                                w.writeheader()
                            w.writerow({k: _csv_cell(r[k]) for k in cols})
                fh.flush()
                fh.detach()
    return buf.getvalue() if out is None else None
