-- A file import's raw keeps only the last four of any run of eight or more
-- digits in two more places, the same widening sync/base.py mask_file_raw
-- now applies on the way in:
--
--   * the cells a row had beyond its header. csv.DictReader files them
--     under the key None as a list (stored as "null"), and an unquoted comma
--     in a bank's last free-text column is exactly how a memo's account
--     number ends up there. The earlier pass (178) read only string cells,
--     so the list kept the number whole;
--   * free-text columns named in the bank's own words. A header is free
--     text when it is one of the exact names 178 used, or when it contains
--     memo, note, description, details, payee, narrative or particulars
--     ("Transaction Details", "Extended Description", "Memo/Notes").
--
-- raw is plaintext JSONB that leaves the instance verbatim through /export
-- and the portability archive. Same cut, once, to the rows already stored;
-- restore applies the live rule to older archives. Keep the three in step:
-- the prefixes are FILE_IMPORT_ID_PREFIXES, the exact keys _FREE_TEXT_KEYS
-- plus _OVERFLOW_KEYS, the words _FREE_TEXT_WORDS.
--
-- A string cell, or a string inside a list cell, is cut when its key is free
-- text or when it holds the row's name or merchant (compared masked,
-- trimmed and case-folded, as the live rule does). Nested objects, numbers
-- and every other cell are left as they were.
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
                CASE
                  WHEN jsonb_typeof(e.value) = 'string'
                   AND (lower(btrim(e.key, E' \t\r\n'))
                            IN ('name', 'memo', 'note', 'notes', 'payee',
                                'description', 'original description',
                                'original statement', '', 'null')
                        OR lower(e.key) ~ '(memo|note|description|details|payee|narrative|particulars)'
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
                  WHEN jsonb_typeof(e.value) = 'array'
                  THEN COALESCE((
                       SELECT jsonb_agg(
                                CASE
                                  WHEN jsonb_typeof(a.v) = 'string'
                                   AND (lower(btrim(e.key, E' \t\r\n'))
                                            IN ('name', 'memo', 'note',
                                                'notes', 'payee',
                                                'description',
                                                'original description',
                                                'original statement', '',
                                                'null')
                                        OR lower(e.key) ~ '(memo|note|description|details|payee|narrative|particulars)'
                                        OR lower(btrim(regexp_replace(
                                               a.v #>> '{}', '\d{4,}(\d{4})',
                                               '…\1', 'g'), E' \t\r\n'))
                                           IN (lower(btrim(regexp_replace(
                                                   t.name, '\d{4,}(\d{4})',
                                                   '…\1', 'g'), E' \t\r\n')),
                                               lower(btrim(regexp_replace(
                                                   t.merchant_name,
                                                   '\d{4,}(\d{4})', '…\1',
                                                   'g'), E' \t\r\n'))))
                                  THEN to_jsonb(regexp_replace(
                                           a.v #>> '{}', '\d{4,}(\d{4})',
                                           '…\1', 'g'))
                                  ELSE a.v END
                                ORDER BY a.n)
                         FROM jsonb_array_elements(e.value)
                              WITH ORDINALITY AS a(v, n)), '[]'::jsonb)
                  ELSE e.value END)
         FROM jsonb_each(t.raw) e)
 WHERE split_part(t.id, ':', 1) IN ('pdf', 'csv', 'ofx', 'qif', 'mint', 'ynab',
                                    'monarch', 'copilot', 'simplifi')
   AND jsonb_typeof(t.raw) = 'object'
   AND t.raw::text ~ '\d{8}'
   AND EXISTS (
       SELECT 1
         FROM jsonb_each(t.raw) e
        CROSS JOIN LATERAL (
              SELECT e.value AS v
               WHERE jsonb_typeof(e.value) = 'string'
              UNION ALL
              SELECT x.v
                FROM jsonb_array_elements(
                       CASE WHEN jsonb_typeof(e.value) = 'array'
                            THEN e.value ELSE '[]'::jsonb END) AS x(v)) c
        WHERE jsonb_typeof(c.v) = 'string'
          AND (c.v #>> '{}') ~ '\d{8}'
          AND (lower(btrim(e.key, E' \t\r\n'))
                   IN ('name', 'memo', 'note', 'notes', 'payee',
                       'description', 'original description',
                       'original statement', '', 'null')
               OR lower(e.key) ~ '(memo|note|description|details|payee|narrative|particulars)'
               OR lower(btrim(regexp_replace(
                      c.v #>> '{}', '\d{4,}(\d{4})', '…\1', 'g'),
                      E' \t\r\n'))
                  IN (lower(btrim(regexp_replace(
                          t.name, '\d{4,}(\d{4})', '…\1', 'g'), E' \t\r\n')),
                      lower(btrim(regexp_replace(
                          t.merchant_name, '\d{4,}(\d{4})', '…\1', 'g'),
                          E' \t\r\n')))));
