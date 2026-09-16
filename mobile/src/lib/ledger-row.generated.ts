// GENERATED from specs/ledger-row.schema.json by
// scripts/gen-ledger-row-types.py — do not edit by hand. Change the
// schema, run the script, and web + mobile + the server move together
// (tests/test_ledger_row_contract.py holds all three to it).

/** One transaction as every ledger surface receives it. */
export interface Txn {
  /** source-stable transaction id */
  id: string;
  /** posted date, ISO */
  date: string;
  /** positive = money out */
  amount: number;
  /** the merchant as the ledger shows it (merchant row name → alias → raw) */
  payee: string;
  /** the merchant ROW (engine/merchant_identity) */
  merchant_id?: string | null;
  /** Plaid's logo for the merchant, when it has one */
  merchant_logo?: string | null;
  /** the effective category in DISPLAY form (spaces, never underscores) */
  category: string;
  /** institution + account name + mask */
  account: string | null;
  mask?: string | null;
  /** 1 = still pending at the bank */
  pending: number;
  /** the verdict's own rule: does this row count as household spend */
  counts_as_spend?: boolean | null;
  /** paired with a reimbursing deposit */
  reimb: boolean;
  /** flagged as reimbursement expected */
  reimb_flag: boolean;
  /** Schedule C tag on a household charge */
  biz_flag: boolean;
  /** the business this row BELONGS to (a business-entity account) */
  entity?: string | null;
  has_receipt: boolean;
  /** free-text per-transaction note */
  note?: string | null;
  /** household owner (per-txn override else the account's) */
  owner?: string | null;
  /** the active bill whose matcher counts this row (⟳) */
  recurring_bill?: string | null;
  /** what a matched store order or receipt contained, by what was bought; null for every other merchant */
  item_summary?: string | null;
  check_number?: string | null;
  /** in store | online | other */
  payment_channel?: string | null;
  /** the payment app in front of the merchant (PayPal, Square…) */
  payment_processor?: string | null;
  location_city?: string | null;
  location_region?: string | null;
  location_address?: string | null;
  location_store?: string | null;
  location_lat?: number | null;
  location_lon?: number | null;
  /** merchant category code */
  mcc?: string | null;
  authorized_date?: string | null;
  /** the bank's own descriptor */
  bank_text?: string | null;
  /** who decided category_primary: plaid | import | flow | fuel | mcc | rule_user | rule_llm | rule_seed | rule_model | rule */
  category_source?: string | null;
  /** the provenance in words, composed by the server */
  category_why?: string | null;
  /** category_primary as stored (a key) */
  category_key?: string | null;
  /** the override as stored, when any */
  category_override?: string | null;
  category_detailed?: string | null;
  /** Plaid's own primary */
  category_plaid?: string | null;
  category_plaid_detailed?: string | null;
  /** VERY_HIGH | HIGH | MEDIUM | LOW */
  category_plaid_confidence?: string | null;
  /** the bill that stamped the override, when a bill did */
  override_bill_id?: string | null;
  /** that bill's payee */
  override_bill?: string | null;
  /** true when a person set the override */
  override_manual?: boolean | null;
  /** true when the row is a companion charge — the fee that rides with override_bill's payment */
  override_is_fee?: boolean | null;
}
