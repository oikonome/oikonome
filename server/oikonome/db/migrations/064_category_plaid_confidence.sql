-- Max-accuracy categories: keep Plaid's PFC as its own columns (updated only
-- by sync), including confidence. Working category_primary may still be
-- refined by seed/LLM/user-merchant rules for LOW/MEDIUM confidence or
-- generic buckets; category_override remains per-txn supreme.
--
-- Backfill from raw.personal_finance_category when present (Plaid rows).

ALTER TABLE transactions
  ADD COLUMN IF NOT EXISTS category_plaid TEXT,
  ADD COLUMN IF NOT EXISTS category_plaid_detailed TEXT,
  ADD COLUMN IF NOT EXISTS category_plaid_confidence TEXT;

UPDATE transactions SET
  category_plaid = raw #>> '{personal_finance_category,primary}',
  category_plaid_detailed = raw #>> '{personal_finance_category,detailed}',
  category_plaid_confidence = raw #>> '{personal_finance_category,confidence_level}'
WHERE raw ? 'personal_finance_category'
  AND category_plaid IS NULL;
