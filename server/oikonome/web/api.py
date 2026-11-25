"""The JSON API the React SPA and the mobile app consume, mounted by
web/app.py as `api.router`. Session-cookie auth (current_user); every
handler runs on an RLS-scoped tenant connection.

The handlers live in one module per area, `web/api_<area>.py`, and every
one of them registers on the single router in `web/api_common.py`
(specs/api-routers.md). ROUTE ORDER is part of the API: Starlette tries
routes in the order they sit on the router and the first match wins. This
module declares that order (`AREAS`) and puts the router's routes in it, so
it holds whichever API module a process happened to import first. It also
keeps the names other code and the tests read through `oikonome.web.api`.
"""

from __future__ import annotations

# The one router every API module registers on; app.py mounts it.
from .api_common import router  # noqa: F401

# The area modules, in route order (see the docstring).
from . import api_ledger  # noqa: E402,F401  (transactions, merchants, categories, rules)
from . import api_accounts  # noqa: E402,F401  (accounts, provider keys, connections, sync jobs)
from . import api_today  # noqa: E402,F401  (Today, lenses, snapshots, activity, alerts, calendar)
from . import api_reimburse  # noqa: E402,F401  (reimbursement pairing)
from . import api_bills  # noqa: E402,F401  (bills, proposals, merchant history)
from . import api_assistant  # noqa: E402,F401  (the assistant)
from . import api_settings  # noqa: E402,F401  (settings, recipients, SMTP test)
from . import api_retirement  # noqa: E402,F401  (retirement projection, debt plan)
from . import api_continuity  # noqa: E402,F401  (the continuity packet)
from . import api_receipts  # noqa: E402,F401  (receipts)
from . import api_access  # noqa: E402,F401  (script tokens, support access)
from . import api_imports  # noqa: E402,F401  (imports, tax documents, onboarding, collector pushes)
from . import api_business  # noqa: E402,F401  (business tagging, entities, tax pack)
from . import api_notify  # noqa: E402,F401  (notification channels, send-now email)

# The declared route order: one area after another, and inside an area the
# order its module wrote them in. A route's handler names its module, so
# this is a stable sort and not a second list to keep in step.
AREAS = (api_ledger, api_accounts, api_today, api_reimburse, api_bills,
         api_assistant, api_settings, api_retirement, api_continuity,
         api_receipts, api_access, api_imports, api_business, api_notify)


def _in_declared_order(routes: list) -> list:
    """`routes` grouped by area in `AREAS` order. A process that imported
    an area module before this one registered that area's routes early;
    left there they would be tried first."""
    place = {m.__name__: i for i, m in enumerate(AREAS)}
    return sorted(routes, key=lambda r: place.get(
        getattr(getattr(r, "endpoint", None), "__module__", ""), len(AREAS)))


router.routes[:] = _in_declared_order(router.routes)


# ---- names other code reads through this module -------------------------
# Shared modules tests patch through `api.<module>` (the patch lands on the
# module itself, so every area sees it).
from ..db import tenancy  # noqa: E402,F401
from ..engine import activity as _activity  # noqa: E402,F401
from . import demoguard  # noqa: E402,F401
# (MONEY_MAX itself, the typed-money reader and the money check live in
# web.fields with the typed body reader; the old names stay importable)
from .fields import (  # noqa: E402,F401
    iso_date as _iso_date,
    json_fields_or_empty as _json_fields_or_empty,
    money_float as _money_float,
    MONEY_MAX as _MONEY_MAX,
    typed_money as _typed_money,
)
# moved to report.bills_remaining_month; alias kept for callers and tests
from .report import (  # noqa: E402,F401
    bills_remaining_month as _bills_remaining_month,
)
# the helpers the area modules share
from .api_common import (  # noqa: E402,F401
    _budgets_set,
    _category_long_ok,
    _category_name,
    _category_why,
    _ck_year,
    _ck_ym,
    _conn,
    _CONTROL_CHARS_RE,
    _CSV_FORMULA,
    _CSV_FORMULA_LEAD,
    _CSV_INVISIBLE,
    _CSV_INVISIBLE_LEAD,
    _CSV_PIECE_BREAK,
    _csv_piece_safe,
    _csv_piece_unsafe,
    _csv_safe,
    _csv_writer,
    _filename_safe,
    _label_field,
    _literal_true,
    _log_proposal,
    _may_edit,
    MERCHANT_NAME_MAX,
    _merchant_name_or_400,
    _new_category_or_400,
    _owner_only,
    _recurring_stamper,
    _SafeCsvWriter,
    _same_site_download,
    _scalar_text,
    _ser,
    _setup_mode_of,
    _SOURCE_WORDS,
    _stamp_recurring,
    _today,
    _today_for,
    _user,
)
from .api_ledger import (  # noqa: E402,F401
    categories,
    categories_rename_api,
    clear_split_api,
    _EXPORT_CHUNK,
    _EXPORT_HELD,
    _EXPORT_HELD_MAX,
    _EXPORT_HELD_PER_HOUSEHOLD,
    _EXPORT_MAX,
    _EXPORT_SEND_PIECE,
    _export_slot,
    _EXPORT_SLOT_TTL,
    _EXPORT_SLOTS,
    _EXPORT_SPOOL_MEMORY,
    _log_bulk,
    _per_day,
    rules_delete_api,
    rules_disable_api,
    rules_set_api,
    set_category,
    set_split_api,
    set_txn_note,
    set_txn_owner_api,
    transactions,
    transactions_bulk,
    TRANSACTIONS_EXPORT_CAP,
    _transactions_payload,
)
from .api_accounts import (  # noqa: E402,F401
    account_set_hidden,
    accounts,
    accounts_add_api,
    accounts_balance_api,
    accounts_classify_api,
    accounts_exclude_api,
    accounts_remove_api,
    accounts_rename_api,
    accounts_simplefin_api,
    connection_disconnect,
    connection_reconnect,
    connections_export,
    connections_import,
    _disconnect_under_sync_lock,
    jobs_sync_api,
    _no_byo_on_hosted,
    _plaid_straggler_ids,
    plaid_validate,
)
from .api_today import (  # noqa: E402,F401
    alerts_dismiss_api,
    alerts_restore_api,
    budget_snapshots,
)
from .api_reimburse import (  # noqa: E402,F401
    reimburse_candidates,
    reimburse_flag,
    reimburse_link,
    reimburse_unflag,
    reimburse_unlink,
)
from .api_bills import (  # noqa: E402,F401
    _adjust_approved_bill,
    _bill_cadence,
    bills_list,
    bills_proposal_api,
    bills_proposals_approve_all,
    bills_restore_api,
    bills_save_api,
    CADENCES,
    _decide_proposal,
    _hold_bill_claims,
    _month_bill_stats,
    _next_on_day,
    _proposal_adjust,
)
from .api_assistant import (  # noqa: E402,F401
    REPORT_BLOCKS,
    REPORT_TURNS,
)
from .api_settings import (  # noqa: E402,F401
    _apply_email_pref,
    _apply_recipient_deltas,
    _DEMO_DOOR_KEYS,
    _drop_stale_readds,
    _enrich_cap_ceiling,
    _hide_people,
    _LLM_DNS_CACHE,
    _llm_is_local,
    _merge_unseen_people,
    _recipient_status,
    _refuse_moved_lists,
    remember_removed_address,
    _SETTINGS_FIELDS,
    settings_outbound_resume,
    settings_set,
    _settings_view,
)
from .api_receipts import (  # noqa: E402,F401
    _parse_receipt_async,
    receipt_details,
    receipt_list,
    receipt_match_by_hand,
    receipt_parse,
    receipt_snap,
    receipt_tag,
    receipt_upload,
)
from .api_access import (  # noqa: E402,F401
    tokens_mint_api,
)
from .api_imports import (  # noqa: E402,F401
    _backfill_status,
    _categorize_after_import,
    coinbase_link_api,
    enrich_run_api,
    import_api,
    import_bulk_run,
    import_mapped_api,
    import_rollback_api,
    import_ynab_accounts_api,
    kept_rows_refusal,
    onboarding_api,
    plan_activity_import_api,
)
from .api_business import (  # noqa: E402,F401
    account_assign_entity,
    business_classify,
    business_combine,
    business_flag,
    compliance_add,
    entities_add_member,
    entities_create,
    entities_delete_forever,
    entities_status,
    entities_update,
    entities_update_member,
    _entity_write,
    equity_record,
    equity_reimburse,
    _require_business,
    se_mileage_add,
    se_vendor_mark,
    txn_assign_entity,
    _year_end_sheet,
)
from .api_notify import (  # noqa: E402,F401
    jobs_email_api,
    notify_phone_api,
    notify_phone_verify_api,
    notify_test_api,
    push_subscribe_api,
    push_unsubscribe_api,
    _valid_push_endpoint,
)
