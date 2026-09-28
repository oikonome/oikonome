-- A file import's names keep only the last four of any run of eight or more
-- digits (sync/base.py upsert_transactions, FILE_IMPORT_ID_PREFIXES): a
-- statement line routinely prints the full account or card number, and the
-- importers cut the row's name out of that line. Migration 148 masked the
-- copy kept under raw.line but not the name beside it, which is the field
-- that actually travels (export, portability archive, categorizer prompt,
-- webhooks, every member's ledger). Same cut, once, to the rows already
-- stored; restore applies it to older archives (sync/restore.py). Keep the
-- three in step, prefixes included.
--
-- Aggregator rows are not touched: their name is the feed's own word and
-- every sync restates it. Row ids are not touched either (they are keys
-- every child row points at); a re-import of an old statement still finds
-- its row by the id it was given then.
--
-- The merchant alias and the rename journal are keyed on the same string
-- and follow it in migration 154 (a separate file because instances that
-- had already recorded this one would never run statements appended to
-- it).
--
-- Idempotent: a masked run is "…" plus four digits, which no longer
-- matches, and only rows that change are written.

UPDATE transactions
   SET name = regexp_replace(name, '\d{4,}(\d{4})', '…\1', 'g'),
       merchant_name = regexp_replace(merchant_name, '\d{4,}(\d{4})',
                                      '…\1', 'g'),
       merchant_name_set_aside = regexp_replace(merchant_name_set_aside,
                                                '\d{4,}(\d{4})', '…\1', 'g')
 WHERE split_part(id, ':', 1) IN ('pdf', 'csv', 'ofx', 'qif', 'mint', 'ynab',
                                  'monarch', 'copilot', 'simplifi')
   AND (name ~ '\d{8}' OR merchant_name ~ '\d{8}'
        OR merchant_name_set_aside ~ '\d{8}');
