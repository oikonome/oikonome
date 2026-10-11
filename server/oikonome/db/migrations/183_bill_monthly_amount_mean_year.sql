-- A weekly or daily bill's monthly figure is its cycle's rate over a mean
-- Gregorian year (engine/bills.PER_YEAR: 365.2425 / 7 weeks, 365.2425
-- days). Bills saved before that rate still store the figure the old
-- 52-week / 365-day year gave, until someone saves them again; the budget's
-- expected income sums the stored column while the Bills headline
-- recomputes it, so the two disagree for every such bill. Restate the
-- stored figure once.
--
-- The cycle is read from raw's recurrence, as the app reads it: the
-- frequency column holds a display alias (EVERY_WEEK, EVERY_X_WEEKS, …),
-- not the cycle name. A missing interval is 1, as bills._monthly treats
-- it; an interval that is not a small positive whole number was never
-- written by the app and is left alone.
--
-- Idempotent: only a row whose stored figure equals the old-rate figure
-- to the cent is written, and the figure it gets differs from that to the
-- cent, so a second run (a bill saved at the new rate, or a figure set by
-- anything else) matches nothing.

WITH cyc AS (
    SELECT b.tenant_id, b.id, b.amount, b.monthly_amount,
           upper(b.raw #>> '{recurrence,frequency}') AS freq,
           CASE WHEN b.raw #>> '{recurrence,interval}' IS NULL THEN 1
                WHEN b.raw #>> '{recurrence,interval}' ~ '^[1-9][0-9]{0,4}$'
                    THEN (b.raw #>> '{recurrence,interval}')::int
           END AS iv
      FROM bills b
     WHERE upper(b.raw #>> '{recurrence,frequency}') IN ('WEEKLY', 'DAILY')
       AND b.amount IS NOT NULL AND b.monthly_amount IS NOT NULL
), rates AS (
    SELECT tenant_id, id, monthly_amount,
           amount * CASE freq WHEN 'WEEKLY' THEN 52.0 ELSE 365.0 END
                  / iv / 12.0 AS old_rate,
           amount * CASE freq WHEN 'WEEKLY' THEN 365.2425 / 7
                              ELSE 365.2425 END
                  / iv / 12.0 AS new_rate
      FROM cyc
     WHERE iv IS NOT NULL
)
UPDATE bills b
   SET monthly_amount = r.new_rate
  FROM rates r
 WHERE b.tenant_id = r.tenant_id AND b.id = r.id
   AND round(r.monthly_amount::numeric, 2) = round(r.old_rate::numeric, 2)
   AND round(r.new_rate::numeric, 2) <> round(r.old_rate::numeric, 2);
