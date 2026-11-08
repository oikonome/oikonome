"""What a restore does to an archive written before a migration.

An export carries rows in the shape the database had when it was written.
A migration that changes stored DATA changes the rows already here, and
nothing else: an archive written before it brings the old shape straight
back, unless the restore applies the same change on the way in. A
migration that ships without saying what a restore does about it lets an
older archive undo it.

So every migration file is accounted for here, by name, in exactly one of
two places, and `tests/test_every_migration_says_what_a_restore_does.py`
fails for a file in neither:

* `UPGRADES`: a migration that changes data in a table the restore
  loads, and how a restore of an older archive gets the same change:

    after       a function the restore calls over the landed rows does it,
                on every archive (the household's own key, for one, is
                never in an archive, so its keying always runs).
    inline      the pass applies the same rule to every archived row, on
                every archive whatever it says about itself: the rule
                binds every door that writes such a row.
    structural  it removed rows or shapes the restore can never plant
                (a parent check, a coercion), or it concerns data an
                archive does not carry.
    gap         nothing in the restore applies it: an older archive's rows
                land in the shape they had. The reason says what that
                costs; a gap is a known debt, written down so it is never
                mistaken for coverage. There are none, and
                `ACCEPTED_GAPS` (empty) is the only way the test lets one
                in.

  There is no "run the migration's file over the household": a restore
  never runs a migration file. That would run PostgreSQL's regular
  expressions over archived text no door has clamped (a back-reference
  can make a few kilobytes take minutes), create temporary functions on
  the household's pooled connection, and be skipped by any archive whose
  manifest claims to be newer than the migration. The passes cut in
  Python instead, as they write.

* `SCHEMA_ONLY`: a migration that changes no stored row a restore loads:
  tables, columns, indexes, functions, policies, grants, or rows of
  control-plane tables no archive restores. The test checks that each
  one writes no restored table.

The archive says how old it is in `manifest.json` (export.build_zip): the
name of the last migration its database had applied. A name this install
does not ship is from a newer one (or an add-on's) and is placed by its
number. An archive without a readable manifest, which is every archive
exported before the manifest existed, says it with its rows instead: a
column a migration added (`FLOOR_COLUMNS`) in a file's header means it was
written at least that late, and an archive showing none is read as the
oldest. What it says is the archive's own claim, and a rule that binds every row
(a card number cut, an amount, a consent) cannot be waived by a line
anyone can edit, so those steps never read it. A step reads it only when
waiving it costs the household nothing but the shape of its own data, and
applying it to a newer archive would undo a choice the person made: 035
and 044 rebuild the household's own category rules, and on a current
archive the derivation could bring back a rule its person deleted.
"""

from __future__ import annotations

import csv
import io
import json
import pathlib
import re
import zipfile
from dataclasses import dataclass

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "db" / "migrations"

#: the archive member naming the schema the archive was written under
MANIFEST = "manifest.json"
#: the manifest's own shape; any other is read as no manifest
MANIFEST_FORMAT = 1
# a manifest is a few hundred bytes; anything larger is not one
_MANIFEST_MAX = 64 * 1024

HOWS = ("after", "inline", "structural", "gap")

#: `gap` entries the test accepts, each with why no restore can apply it.
#: Empty: every migration that changes restored data is applied.
ACCEPTED_GAPS: dict[str, str] = {}


@dataclass(frozen=True)
class Upgrade:
    migration: str
    how: str
    why: str


def migration_names() -> list[str]:
    """Every migration file, in the order `db.migrate` applies them."""
    return sorted(p.name for p in MIGRATIONS.glob("*.sql"))


def manifest(schema: str | None, version: str, tables: list[str]) -> str:
    """The manifest an export writes: the schema its rows are in."""
    return json.dumps({"format": MANIFEST_FORMAT, "schema": schema,
                       "app_version": version, "tables": list(tables)},
                      indent=2) + "\n"


#: Columns a migration added to a table an archive carries, as
#: (migration, archive member, column): an archive whose member's header
#: shows the column was written by an install that had run at least that
#: migration. What an archive without a manifest is read as (archive_floor).
#: Only a column the export writes whenever the table has a row counts, and
#: only one no earlier migration or schema had on that table.
FLOOR_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("064_category_plaid_confidence.sql", "transactions.csv",
     "category_plaid"),
    ("077_income_document_ein_last4.sql", "income_documents.csv",
     "ein_last4"),
    ("089_entity_archive_timestamp.sql", "business_entity.csv",
     "archived_at"),
    ("092_merchant_outlet.sql", "transactions.csv", "merchant_outlet"),
    ("097_merchants.sql", "transactions.csv", "merchant_id"),
    ("098_txn_enrichment.sql", "transactions.csv", "payment_processor"),
)

# a header line is a few hundred bytes; the read stops well past that
_HEADER_MAX = 64 * 1024
# a migration file's name: its number, an optional letter, then the rest
_NAME_RE = re.compile(r"^([0-9]{1,9})([a-z]?)_[A-Za-z0-9_.-]+$")


def _header(z: zipfile.ZipFile, member: str) -> set[str]:
    """The column names of a CSV member's first line, or none."""
    try:
        with z.open(member) as fh:
            head = fh.read(_HEADER_MAX)
    except (KeyError, zipfile.BadZipFile, OSError, RuntimeError):
        return set()
    # read as the restore's own row reader reads the file (newline="", so
    # csv ends the line at CR, LF or CRLF alike): a file it would restore
    # must not fail here first
    try:
        first = next(csv.reader(io.TextIOWrapper(
            io.BytesIO(head), encoding="utf-8-sig", newline="")), [])
    except (csv.Error, UnicodeDecodeError):
        return set()
    return {c.strip() for c in first}


def archive_floor(z: zipfile.ZipFile) -> str | None:
    """The newest migration a column in the archive's headers shows it
    had (FLOOR_COLUMNS), or None when it shows none."""
    floor = None
    headers: dict[str, set[str]] = {}
    for migration, member, column in FLOOR_COLUMNS:
        if member not in headers:
            headers[member] = _header(z, member)
        if column in headers[member] and (
                floor is None or _order(floor) < _order(migration)):
            floor = migration
    return floor


def _manifest_schema(z: zipfile.ZipFile) -> str | None:
    """What a readable manifest names, if it names a migration file."""
    try:
        info = z.getinfo(MANIFEST)
    except KeyError:
        return None
    if info.file_size > _MANIFEST_MAX:
        return None
    try:
        doc = json.loads(z.read(MANIFEST).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, zipfile.BadZipFile):
        return None
    if not isinstance(doc, dict) or doc.get("format") != MANIFEST_FORMAT:
        return None
    schema = doc.get("schema")
    if not isinstance(schema, str) or not _NAME_RE.match(schema):
        return None
    return schema


def archive_schema(z: zipfile.ZipFile) -> str | None:
    """How new the archive is: the last migration its database had applied,
    as its manifest names it (a name this install does not ship included:
    one from a newer install, or an add-on's, placed by its number), or,
    where its rows show a later one or it has no manifest it can read, the
    newest migration its headers show (archive_floor). None when neither
    says anything: read as the oldest, so every step runs."""
    named = _manifest_schema(z)
    floor = archive_floor(z)
    if named is None or (floor is not None and _order(named) < _order(floor)):
        return floor
    return named


def _order(name: str) -> tuple:
    """A migration name's place in the order `db.migrate` applies the files
    (by name; the numbers are zero-padded), read by its number so a name
    this install does not ship still sorts where it belongs."""
    m = _NAME_RE.match(name)
    return (int(m.group(1)), m.group(2), name) if m else (-1, "", name)


def older_than(schema: str | None, migration: str) -> bool:
    """Was an archive at `schema` written before `migration` ran?"""
    return schema is None or _order(schema) < _order(migration)


UPGRADES: tuple[Upgrade, ...] = (
    Upgrade("022_honest_push_aggregators.sql", "inline",
            "_push_fed_items: a 'csv' connection named coinbase-<label>, or "
            "whose every account is <connection>-<plan>, lands as its push "
            "door's and archived"),
    Upgrade("035_canonical_category_backfill.sql", "after",
            "derive_user_rules_from_overrides over the merchants of the "
            "corrected rows landed, for an archive older than it "
            "(archive_schema)"),
    Upgrade("040_bills_rename.sql", "inline",
            "the bills pass reads recurring.csv when there is no bills.csv"),
    Upgrade("044_llm_rule_canonical_rekey.sql", "after",
            "rekey_model_rules_on_the_merchant, for an archive older than it "
            "(archive_schema)"),
    Upgrade("054_business_entity.sql", "structural",
            "a pointer to a business the restore does not hold is dropped "
            "(_entity)"),
    Upgrade("064_category_plaid_confidence.sql", "inline",
            "upgrade_archived_txn: Plaid's category from "
            "raw.personal_finance_category where the row has none"),
    Upgrade("067_txn_child_fks.sql", "structural",
            "a child whose transaction is not here is refused as orphaned "
            "(_txn_exists)"),
    Upgrade("071_receipt_txn_fk.sql", "structural",
            "a receipt naming a transaction that is not here is refused as "
            "orphaned"),
    Upgrade("074_recipient_invites.sql", "after",
            "_restore_recipient_invites: an archive with no answers member, "
            "older than 074 (archive_schema), records its configured "
            "recipients who hold an account here as agreed, with no link; anyone else must be invited (a file "
            "never enrols an outside mailbox)"),
    Upgrade("075_card_payment_normalize.sql", "inline",
            "upgrade_archived_txn, with the ingest's own card-payment test"),
    Upgrade("076_receipt_items_fk.sql", "structural",
            "a line whose receipt is not here is refused as orphaned"),
    Upgrade("077_income_document_ein_last4.sql", "inline",
            "_ein_last4_for_restore keeps the last four, never the number"),
    Upgrade("081_venmo_counterparty_merchant.sql", "inline",
            "upgrade_archived_txn (base.venmo_counterparty) on a row the "
            "archive names no merchant row for"),
    Upgrade("082_zelle_counterparty_merchant.sql", "inline",
            "upgrade_archived_txn (base.zelle_counterparty)"),
    Upgrade("083_venmo_merchant_keeps_rail.sql", "inline",
            "upgrade_archived_txn: a bare parse takes the rail"),
    Upgrade("087_budget_snapshot_source.sql", "inline",
            "a snapshot without a source restores as 'close'"),
    Upgrade("089_entity_archive_timestamp.sql", "inline",
            "an archived business without a date restores dated by its "
            "updated_at"),
    Upgrade("091_fuel_arm_merchant.sql", "inline",
            "upgrade_archived_txn: 092 takes back off what 091 wrote, and "
            "the pump goes in merchant_outlet (093)"),
    Upgrade("092_merchant_outlet.sql", "inline",
            "upgrade_archived_txn: the fuel suffix comes off merchant_name, "
            "and the outlet is derived by base.fuel_arm"),
    Upgrade("093_fuel_arm_any_brand.sql", "inline",
            "upgrade_archived_txn (base.fuel_arm is 093's rule)"),
    Upgrade("094_fuel_arm_recategorize.sql", "inline",
            "upgrade_archived_txn: a row with an outlet is filed as fuel, "
            "unless a person or a rule decided its category"),
    Upgrade("098_txn_enrichment.sql", "inline",
            "upgrade_archived_txn: base.enrichment_from_raw fills the empty "
            "columns, and category_source is derived as 098 derived it"),
    Upgrade("100_account_links_account_fk.sql", "structural",
            "a link naming an account that is not here is refused as "
            "orphaned"),
    Upgrade("125_item_lists_stay_arrays.sql", "structural",
            "store item lists are read through _json_list"),
    Upgrade("127_override_source.sql", "after",
            "categories.backfill_override_source over the landed rows"),
    Upgrade("128_raw_is_an_object.sql", "structural",
            "every raw cell is read through _raw_dict / clean_bill_raw"),
    Upgrade("132_transactions_raw_is_an_object.sql", "structural",
            "every raw cell is read through _raw_dict"),
    Upgrade("137_merchant_name_set_aside.sql", "inline",
            "_merchant_name_cells moves an overruled name aside"),
    Upgrade("146_raw_identifier_scrub.sql", "inline",
            "scrub_account_raw / scrub_liability_raw / "
            "scrub_transaction_raw, a file row carrying the app's stand-in "
            "note included; an aggregator's raw keeps its own fields"),
    Upgrade("147_account_ids_hold_no_comma.sql", "structural",
            "every account id is read through _account_id"),
    Upgrade("148_raw_statement_line_scrub.sql", "inline",
            "scrub_transaction_raw cuts the statement line"),
    Upgrade("149_account_ids_lose_their_comma.sql", "inline",
            "_account_id on every account id, _rename_account_keys on "
            "settings and snapshots"),
    Upgrade("150_link_made_category_marker.sql", "after",
            "classify_link_made_pins, for an archive without the mark"),
    Upgrade("151_account_ids_lose_their_comma_when_taken.sql", "inline",
            "_plan_comma_renames"),
    Upgrade("153_file_import_name_number_scrub.sql", "inline",
            "_txn_params cuts a file row's names"),
    Upgrade("154_file_import_alias_scrub.sql", "inline",
            "_alias_where / _renamed_key key aliases and the journal on the "
            "cut spelling"),
    Upgrade("155_receipts_wait_for_their_transaction.sql", "structural",
            "the receipts pass screens match_method; only a receipt waits"),
    Upgrade("157_withdrawn_charges_before_the_stamp.sql", "after",
            "base.stamp_unstamped_feed_withdrawals over the landed rows"),
    Upgrade("158_disconnected_items_forget_credentials.sql", "structural",
            "a restored connection carries no credential"),
    Upgrade("159_one_fronted_movement_per_transaction.sql", "structural",
            "the equity pass refuses a second fronted movement as present"),
    Upgrade("159a_fronted_movement_follows_the_booked_entity.sql", "inline",
            "the equity pass keeps the movement for the booked business"),
    Upgrade("160_one_fronted_movement_everywhere.sql", "inline", "as 159a"),
    Upgrade("161_imported_rows_note_what_the_feed_relies_on.sql", "inline",
            "scrub_transaction_raw (stood_in_from_legacy_copies)"),
    Upgrade("163_adoptions_remember_where_history_starts.sql", "structural",
            "restored lineage lands reconciled, with no cutoff"),
    Upgrade("164_vendor_marks_follow_their_merchant.sql", "inline",
            "_mark_merchant: a mark naming no merchant takes the one live "
            "merchant of its name that no other mark of the business holds"),
    Upgrade("167_adoption_floor_is_where_the_feed_begins.sql", "structural",
            "as 163"),
    Upgrade("168_vendor_merge_notes_stay_arrays.sql", "structural",
            "merged_from is read as a list of objects"),
    Upgrade("169_imported_rows_the_overlap_log_names_are_kept.sql",
            "structural", "reads the sync log, which no archive restores"),
    Upgrade("170_adoption_floor_ignores_what_the_new_link_sent.sql",
            "structural", "as 163"),
    Upgrade("171_a_partial_overlap_line_keeps_every_earlier_import.sql",
            "structural", "as 169"),
    Upgrade("172_stand_in_notes_name_the_adopted_account.sql", "inline",
            "base.stood_in_for_restore keeps only accounts held here"),
    Upgrade("173_a_vendor_mark_names_a_merchant_that_exists.sql",
            "structural", "_known_merchant drops a dangling merchant id"),
    Upgrade("177_one_household_member_per_business.sql", "inline",
            "the membership pass marks one household member per business"),
    Upgrade("178_file_import_raw_number_scrub.sql", "inline",
            "base.mask_file_raw"),
    Upgrade("181_file_import_raw_overflow_and_free_text_scrub.sql", "inline",
            "base.mask_file_raw"),
    Upgrade("183_bill_monthly_amount_mean_year.sql", "inline",
            "bills.restated_monthly in the bills pass"),
    Upgrade("184_split_source.sql", "inline",
            "a part without a source restores as the person's"),
    Upgrade("188_import_batch_filename_number_scrub.sql", "inline",
            "card_number.cut on the file name"),
    Upgrade("189_manual_account_name_number_scrub.sql", "inline",
            "card_number.cut on a hand-made account's names"),
    Upgrade("193_adoption_guard_remembers_what_each_page_matched.sql",
            "structural", "as 163"),
    Upgrade("199_split_account_ids_and_source_cells_lose_full_numbers.sql",
            "after", "base.rekey_numbered_accounts, and mask_file_raw's "
            "split-source cells inline"),
    Upgrade("202_file_row_ids_on_numbered_accounts_are_keyed.sql", "after",
            "base.key_file_import_ids"),
    Upgrade("203_grouped_card_numbers_stored_on_file_rows_are_cut.sql",
            "inline", "card_number.cut in the passes: a file row's names, "
            "outlet and raw cells (_txn_params, mask_file_raw), the import "
            "file name, and the alias, journal and rule keys that follow "
            "them (_alias_where)"),
    Upgrade("204_card_numbers_in_merchant_names_are_cut.sql", "inline",
            "card_number.cut in the passes: a machine's merchant names "
            "and aliases, the journal, the rules and 1099 marks that "
            "follow a merchant's name; a person's names stay"),
    Upgrade("205_a_transaction_always_has_an_amount.sql", "inline",
            "_txn_params lands a blank amount at 0 and marks the row"),
    Upgrade("206_card_numbers_in_bills_offers_and_the_activity_log_are_cut"
            ".sql", "inline",
            "card_number.cut in the passes: a detected bill lands under "
            "its cut name's id (onto the bill the household already held "
            "there; beside it, under its own id, when that is a bill a "
            "person made or one this restore landed), and "
            "its pins, offers and settings follow (_offer_cut, "
            "_follow_moved_bills); offers' labels and the activity log"),
    Upgrade("207_account_sources_say_which_account_a_source_fills.sql",
            "after", "account_sources.backfill: 207's digest, over the "
            "restored hand-made accounts, after pruning every c-row not "
            "true of its account's cut stored name, and never over a name "
            "that still spells a number"),
    Upgrade("208_card_numbers_in_bill_keys_and_log_names_are_cut.sql",
            "after", "bills.cut_detected_keys and activity.cut_stored_names "
            "over the restored household, after its bills, merchants and "
            "log have landed: a detected bill's match key and identity "
            "names, the log's name targets and the names its details quote "
            "(the migration's own cut is a Python step too, never SQL)"),
    Upgrade("209_card_numbers_in_import_file_names_are_cut.sql", "inline",
            "card_number.cut_as_slug_reads on the import file name"),
    Upgrade("210_payee_keyed_settings_follow_a_cut_bill_name.sql", "after",
            "bills.follow_cut_payees, which bills.cut_detected_keys runs "
            "over the restored household after its bills and settings "
            "landed: a setting naming a detected bill by the number its "
            "payee was cut from moves onto the cut payee (an older "
            "archive's moved bills are followed by _follow_moved_bills)"),
    Upgrade("214_plaid_raw_keeps_no_account_or_person_identifiers.sql",
            "inline", "scrub_account_raw / scrub_liability_raw / "
            "scrub_transaction_raw and the holdings pass drop "
            "plaid.PLAID_RAW_IDENTIFIERS from every archived raw"),
)


#: Migrations that change no stored row a restore loads. The test checks
#: each: no INSERT, UPDATE or DELETE on a restored table.
SCHEMA_ONLY: dict[str, str] = {
    **{name: "tables, columns, indexes, functions, policies or grants only"
       for name in (
           "001_tenant_keys.sql",
           "002_import_batches.sql",
           "003_nullif_tenant_guc.sql",
           "004_testing_feedback.sql",
           "005_account_type_user_set.sql",
           "006_password_resets_session_ids.sql",
           "007_business_flags.sql",
           "008_history_tables.sql",
           "009_script_heartbeats.sql",
           "010_feedback_reports.sql",
           "011_session_ip.sql",
           "012_family_roles.sql",
           "013_passkeys.sql",
           "014_account_links.sql",
           "015_receipts.sql",
           "016_job_progress.sql",
           "017_api_tokens.sql",
           "018_partial_reimbursement.sql",
           "019_import_staging.sql",
           "020_heartbeat_via.sql",
           "021_heartbeat_kind.sql",
           "023_signup_gate.sql",
           "026_admin_console.sql",
           "027_recovery_codes.sql",
           "029_deployments.sql",
           "030_broadcast.sql",
           "031_broadcast_deadline.sql",
           "032_totp_replay.sql",
           "034_deployment_summary.sql",
           "036_rule_disabled.sql",
           "037_owner_attribution.sql",
           "038_receipt_optimized.sql",
           "039_receipt_kind.sql",
           "041_plaid_webhook_status.sql",
           "042_plaid_item_ledger.sql",
           "043_plaid_identifiers.sql",
           "045_infra_metrics.sql",
           "046_item_reaper.sql",
           "048_drop_testing_checklist.sql",
           "050_notify_channels.sql",
           "051_tenant_ga_residuals.sql",
           "052_delete_prev_status.sql",
           "055_equity_movement.sql",
           "056_business_txn_class.sql",
           "057_compliance_obligation.sql",
           "058_selfemploy.sql",
           "059_transaction_notes.sql",
           "061_ops_state.sql",
           "066_account_remove.sql",
           "068_login_unlocks.sql",
           "072_admin_passkeys.sql",
           "073_email_delivery_state.sql",
           "078_entity_tax_reserve.sql",
           "079_item_tx_update_status.sql",
           "084_device_tokens.sql",
           "085_device_push_tokens.sql",
           "086_budget_snapshots.sql",
           "088_login_unlock_refund_once.sql",
           "090_tenant_categorizer_model.sql",
           "095_merchant_renames.sql",
           "096_bill_txn_category.sql",
           "097_merchants.sql",
           "099_logo_cache.sql",
           "102_second_factor_waived.sql",
           "103_admin_audit_index.sql",
           "105_account_adoptions.sql",
           "107_item_bank_update.sql",
           "109_household_member_role.sql",
           "110_rls_initplan.sql",
           "111_transaction_search.sql",
           "114_job_runs_zone.sql",
           "115_budget_snapshot_bills.sql",
           "116_session_elevation.sql",
           "117_signup_reclaims.sql",
           "118_reclaim_password_is_the_clickers.sql",
           "120_elevation_remembers_its_passkey.sql",
           "121_feedback_reports_carry_a_kind.sql",
           "122_factor_resets_cooling_off.sql",
           "123_costco_receipts.sql",
           "124_infra_metrics_pg_max_conns.sql",
           "130_retired_at.sql",
           "131_signup_invite_options.sql",
           "136_merchant_name_overruled.sql",
           "138_transaction_splits.sql",
           "139_activity_log.sql",
           "140_outbound_integrations.sql",
           "141_widget_tokens.sql",
           "142_alert_mute_changed_at.sql",
           "144_mint_ticket_closes_its_session.sql",
           "145_alert_snapshot_seen_at.sql",
           "152_sync_log_last_ok_by_item.sql",
           "156_item_resync_from.sql",
           "162_rename_ids_sequence_is_not_the_apps_to_move.sql",
           "165_rename_journal_keeps_the_rules_it_moved.sql",
           "166_web_push_belongs_to_its_session.sql",
           "174_console_token_sessions_are_bound_on_the_server.sql",
           "175_a_grace_purge_claim_is_its_own_marker.sql",
           "176_a_member_is_the_household.sql",
           "179_bulk_import_runs_in_the_background.sql",
           "182_bulk_import_notes_is_a_list.sql",
           "185_assistant_turns.sql",
           "187_sessions_remember_their_passkey.sql",
           "190_merged_adoptions_remember_where_the_link_began.sql",
           "191_email_change_revert_links.sql",
           "194_plaid_link_sessions_and_release_queue.sql",
           "196_login_email_change_waits_for_the_new_mailbox.sql",
           "197_email_change_requests_record_the_mail_sent.sql",
           "198_a_person_can_turn_household_push_off.sql",
           "200_category_undo_is_held_by_the_server.sql",
           "201_sync_log_newest_success.sql",
           "211_a_folder_import_job_names_the_worker_that_holds_it.sql",
           "212_sync_log_reads_one_connection_from_an_index.sql",
           "213_a_link_session_remembers_the_window_it_asked_for.sql",
       )},
    "060_spam_defense.sql":
        "writes only blocklist_meta (the instance's spam blocklist), which no archive restores",
    "126_one_cooling_off_clock_per_account.sql":
        "writes only factor_resets (sign-in factor resets), which no archive restores",
    "133_first_verified_at.sql":
        "writes only users (the household roster), which no archive restores",
    "143_webhooks_created_by_user.sql":
        "writes only webhooks (webhook registrations), which no archive restores",
    "186_assistant_threads.sql":
        "writes only assistant_turns (assistant threads), which no archive restores",
    "192_email_change_revert_links_name_their_household.sql":
        "writes only email_change_reverts (email-change revert links), which no archive restores",
    "195_verification_links_name_their_mailbox.sql":
        "writes only email_verifications (email verification links), which no archive restores",
}
