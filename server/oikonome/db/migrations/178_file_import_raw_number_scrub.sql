-- A file import's raw keeps only the last four of any run of eight or more
-- digits in the cells that held the row's name or merchant, and in the
-- free-text cells a statement prints numbers into (memo, note, payee,
-- description, …) — sync/base.py mask_file_raw. Migration 153 cut the
-- name, but raw is plaintext JSONB that leaves the instance verbatim
-- through /export and the portability archive, so the cell the name was
-- cut from still carried the full account or card number. Same cut, once,
-- to the rows already stored; restore applies it to older archives
-- (sync/restore.py). Keep the three in step: the prefixes are
-- FILE_IMPORT_ID_PREFIXES, the keys _FREE_TEXT_KEYS.
--
-- A cell is matched to the name by value, compared masked and trimmed and
-- case-folded, as the live rule does: the stored name is already masked,
-- while the cell still holds it whole. Only top-level string cells are
-- touched; every other cell is left as it was.
--
-- Aggregator rows are not touched: their raw is the feed's own record and
-- every sync restates it.
--
-- Bounded and idempotent: only file rows holding a run of eight or more
-- digits in a cell the rule covers are written, and a masked run is "…"
-- plus four digits, which no longer matches.

UPDATE transactions t
   SET raw = (
       SELECT jsonb_object_agg(
                e.key,
                CASE WHEN jsonb_typeof(e.value) = 'string'
                      AND (lower(btrim(e.key, E' \t\r\n'))
                               IN ('name', 'memo', 'note', 'notes', 'payee',
                                   'description', 'original description',
                                   'original statement')
                           OR lower(btrim(regexp_replace(
                                  e.value #>> '{}', '\d{4,}(\d{4})', '…\1',
                                  'g'), E' \t\r\n'))
                              IN (lower(btrim(regexp_replace(
                                      t.name, '\d{4,}(\d{4})', '…\1', 'g'),
                                      E' \t\r\n')),
                                  lower(btrim(regexp_replace(
                                      t.merchant_name, '\d{4,}(\d{4})',
                                      '…\1', 'g'), E' \t\r\n'))))
                     THEN to_jsonb(regexp_replace(
                              e.value #>> '{}', '\d{4,}(\d{4})', '…\1', 'g'))
                     ELSE e.value END)
         FROM jsonb_each(t.raw) e)
 WHERE split_part(t.id, ':', 1) IN ('pdf', 'csv', 'ofx', 'qif', 'mint', 'ynab',
                                    'monarch', 'copilot', 'simplifi')
   AND jsonb_typeof(t.raw) = 'object'
   AND t.raw::text ~ '\d{8}'
   AND EXISTS (
       SELECT 1 FROM jsonb_each(t.raw) e
        WHERE jsonb_typeof(e.value) = 'string'
          AND (e.value #>> '{}') ~ '\d{8}'
          AND (lower(btrim(e.key, E' \t\r\n'))
                   IN ('name', 'memo', 'note', 'notes', 'payee',
                       'description', 'original description',
                       'original statement')
               OR lower(btrim(regexp_replace(
                      e.value #>> '{}', '\d{4,}(\d{4})', '…\1', 'g'),
                      E' \t\r\n'))
                  IN (lower(btrim(regexp_replace(
                          t.name, '\d{4,}(\d{4})', '…\1', 'g'), E' \t\r\n')),
                      lower(btrim(regexp_replace(
                          t.merchant_name, '\d{4,}(\d{4})', '…\1', 'g'),
                          E' \t\r\n')))));
