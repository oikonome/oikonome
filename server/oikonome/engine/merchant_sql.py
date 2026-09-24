"""The ONE spelling of "the merchant of this transaction" in SQL.

Every surface that names a merchant joins through these fragments —
ledger, email, bills history, spending, books, merchants catalog. Callers
alias transactions as `t`, add MC_JOIN, and label by DISPLAY_MERCHANT.
Import-free on purpose so budget.py (which merchant_dedup imports) can use
it too; test_merchant_dedup's drift test forbids any other module from
spelling the key or the join itself.

The merchant is a ROW (`merchants`, joined as `mm` on
transactions.merchant_id — engine/merchant_identity.py resolves it once
per row). mm.name is the display name; merchant_canonical.canonical is the
alias mirror kept for rows not yet resolved.

`raw_key` is the IDENTITY KEY: the string a row is grouped, aliased and
re-resolved by. The outlet comes first when a chain's fuel arm was
identified, then the aggregator's merchant name, then the bank's own
descriptor. The alias join keys on the OUTLET, not on merchant_name: a
merge row that maps a brand to itself would otherwise outrank the fuel-arm
outlet and collapse the pump back into the brand.

Raw key → merchant must stay a FUNCTION — rename, reconcile, merge, split
repair and a whole-ledger re-resolve all read it that way, so a row the
resolver moves off its aggregator name must stop answering to it. It does,
without anything here knowing: the name is MOVED to
transactions.merchant_name_set_aside and merchant_name left NULL, so the
row reads as one the aggregator never named. The reasoning is in
specs/merchant-identity.md.
"""


def raw_key(alias: str = "t") -> str:
    """The identity key of a transaction row, for one table alias.

    `alias` is how the query names the transactions table ("t",
    "transactions"), or "" where its columns stand unqualified."""
    p = f"{alias}." if alias else ""
    return f"COALESCE({p}merchant_outlet, {p}merchant_name, {p}name)"


RAW_KEY = raw_key()
RAW_KEY_UNALIASED = raw_key("")

MC_JOIN = (f"LEFT JOIN merchant_canonical mc ON mc.raw_merchant = {RAW_KEY} "
           "LEFT JOIN merchants mm ON mm.id = t.merchant_id")
DISPLAY_MERCHANT = f"COALESCE(mm.name, mc.canonical, {RAW_KEY})"
MERCHANT_LOGO = "mm.logo_url"
MERCHANT_ID = "mm.id"
