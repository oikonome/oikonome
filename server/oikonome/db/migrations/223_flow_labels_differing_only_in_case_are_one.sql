-- A file import files a deposit its bank labels "Income" under the
-- canonical INCOME (sync.flowmap.flow_category). Imports before that rule
-- stored the bank's own spelling, and every reader of income — the
-- month's income, the paycheck tile, the spend exclusions (categories
-- FLOW_SQL) — matches the literal INCOME, so such a deposit was money in
-- that counted as nobody's pay and as negative spending.
--
-- This rewrites exactly the stored rows today's importer would label
-- INCOME for the label alone: money in (amount < 0, the engine's sign)
-- whose category_primary is INCOME in another case, spelled in ASCII
-- letters (the importer folds with Python's lower(), which a database
-- collation's upper() or lower() may not agree with outside ASCII). An
-- outflow filed
-- under the label is a correction or a reversal, which the importer
-- leaves as written, and so does this. category_override is a person's
-- own word and is never touched; category_source stays as it was (an
-- import that writes INCOME today records 'import' too).
--
-- Bounded and idempotent: only a row whose label differs from INCOME is
-- written, and a written row no longer matches.

UPDATE transactions
   SET category_primary = 'INCOME'
 WHERE amount < 0
   AND category_primary <> 'INCOME'
   AND category_primary ~ '^[A-Za-z]+$'
   AND lower(category_primary) = 'income';
