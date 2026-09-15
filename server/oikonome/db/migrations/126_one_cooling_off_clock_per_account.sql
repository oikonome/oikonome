-- At most one live cooling-off clock per account.
--
-- The factor-clearing reset is only bearable because the account can stop
-- it: one click on the mailed link, or any sign-in. A second live request
-- makes that promise false — the person cancels the clock they were told
-- about while another one, which they never heard of, runs on to its own
-- date and clears the factors for whoever asked. Recording a request
-- already supersedes any earlier live row, but check-then-act supersedes
-- nothing when two requests are in flight together: neither transaction
-- can see the other's uncommitted insert, so each cancels nothing and each
-- commits a clock. The application now serializes those on the account;
-- this index states the same rule where no caller can route around it,
-- including a caller written later.
--
-- Rows that already violate it: an account carrying several live requests
-- keeps the one that lands LAST and has the others marked superseded.
-- With one fixed cooling-off period that is also the newest request — the
-- state the same requests would have reached arriving one after another,
-- and the one whose date and cancel link the last notice carried — but
-- stating it as the furthest-out clock means this can never shorten
-- anyone's wait, which is the whole point of the rule it is repairing.
-- Cancelling them all instead would silently discard a wait someone may
-- be six days into. An account where one of two live rows was already
-- cancelled has one live row and is left alone.

WITH live AS (
    SELECT id,
           row_number() OVER (PARTITION BY user_id
                              ORDER BY lands_at DESC, requested_at DESC,
                                       id DESC) AS n
      FROM factor_resets
     WHERE cancelled_at IS NULL AND consumed_at IS NULL
)
UPDATE factor_resets f
   SET cancelled_at = now(), cancel_reason = 'superseded'
  FROM live
 WHERE f.id = live.id AND live.n > 1;

CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_resets_one_live_per_user
    ON factor_resets (user_id)
 WHERE cancelled_at IS NULL AND consumed_at IS NULL;
