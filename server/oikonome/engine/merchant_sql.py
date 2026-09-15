"""The ONE spelling of "the merchant of this transaction" in SQL.

Every surface that names a merchant joins through these fragments —
ledger, email, bills history, spending, books, merchants catalog. Callers
alias transactions as `t`, add MC_JOIN, and label by DISPLAY_MERCHANT.
Import-free on purpose so budget.py (which merchant_dedup imports) can use
it too; test_merchant_dedup's drift test forbids any other module from
spelling the join itself.

Since migration 097 the merchant is a ROW (`merchants`, joined as `mm` on
transactions.merchant_id — engine/merchant_identity.py resolves it once
per row). mm.name is the display name; merchant_canonical.canonical is the
alias mirror kept for rows not yet resolved. The alias join keys on the
OUTLET when there is one, not on merchant_name: a merge row that maps a
brand to itself would otherwise outrank the fuel-arm outlet and collapse
the pump back into the brand.
"""

MC_JOIN = ("LEFT JOIN merchant_canonical mc "
           "ON mc.raw_merchant = COALESCE(t.merchant_outlet, "
           "t.merchant_name, t.name) "
           "LEFT JOIN merchants mm ON mm.id = t.merchant_id")
DISPLAY_MERCHANT = ("COALESCE(mm.name, mc.canonical, t.merchant_outlet, "
                    "t.merchant_name, t.name)")
MERCHANT_LOGO = "mm.logo_url"
MERCHANT_ID = "mm.id"
