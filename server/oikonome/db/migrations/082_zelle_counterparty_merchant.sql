-- Zelle counterparties become their own merchants (companion to 081's
-- Venmo pass).
--
-- Zelle statement lines name BOTH ends of the transfer:
--   NOW Withdrawal — Zelle payment from Sam Owner (Bank Spending Account
--   XXXXXX1234) to CASEY EXAMPLE
-- and imports stored that whole line as the row's name — often truncated
-- into merchant_name — so every Zelle transfer displayed as the same
-- unreadable descriptor and no counterparty ever grouped or totalled.
--
-- sync/base.py (zelle_counterparty) now derives the merchant on ingest;
-- this applies the same transform to stored rows. The amount sign picks
-- the counterparty end — money out (amount > 0, engine sign) is the "to"
-- side, money in the "from" side — so the account holder's own name never
-- needs to be known. Cleanup mirrors the Python parser: drop the own
-- side's "(Bank … Account …)" parenthetical and a trailing ACH "RECEIVER"
-- tag, title-case ALL-CAPS names, keep "LLC" upper. The merchant keeps
-- the rail visible — "Zelle — Casey Example" — since the same person via
-- Venmo and via Zelle are different things to a reader and to grouping.
--
-- Scope: rows whose merchant is absent OR is itself the Zelle descriptor
-- (that truncation is noise, not a merchant). Lines truncated before
-- " to " name nobody and stay as they are. category fields and
-- category_override untouched.
UPDATE transactions t
   SET merchant_name = x.counterparty
  FROM (
    SELECT tenant_id, id,
           'Zelle — ' || REGEXP_REPLACE(
             CASE WHEN c ~ '[a-z]' THEN c ELSE INITCAP(c) END,
             '\mLlc\M', 'LLC', 'g') AS counterparty
      FROM (
        SELECT tenant_id, id,
               NULLIF(TRIM(BOTH ' ,' FROM
                 REGEXP_REPLACE(REGEXP_REPLACE(side, '\s*\(.*$', ''),
                                '\s+RECEIVER\s*$', '', 'i')), '') AS c
          FROM (
            SELECT tenant_id, id,
                   CASE WHEN amount < 0
                        THEN SUBSTRING(src FROM
                               '(?i)zelle payment from (.+?) to ')
                        ELSE SUBSTRING(src FROM
                               '(?i)zelle payment from .+? to (.+)$')
                   END AS side
              FROM (SELECT tenant_id, id, amount, merchant_name,
                           COALESCE(raw->>'original_description', name) AS src
                      FROM transactions) s0
             WHERE (merchant_name IS NULL
                    OR merchant_name ~* 'zelle payment from')
               AND src ~* 'zelle payment from .+ to .+'
          ) s1
      ) s2
  ) x
 WHERE t.tenant_id = x.tenant_id AND t.id = x.id
   AND x.counterparty IS NOT NULL;
