-- An email-verification link speaks for the mailbox it was sent to.
--
-- A link proves one thing: that the address it was mailed to receives
-- this instance's mail. A link bound only to the account would have to
-- burn every older link whenever a new one is minted — otherwise a link
-- mailed to an address the account has since left would verify whatever
-- address the account is at now. Burning older links would make every
-- earlier mail (a resend, the day-7 reminder, the freeze notice) fail as
-- already used once a later one is sent.
--
-- Recording the address each link was sent to binds it properly: a link
-- verifies only while the account is still at that address, so links no
-- longer need to burn each other, and any unexpired, unspent link to the
-- mailbox works. Spending stays one-use per link.
--
-- Backfill: a link minted before this column existed takes its account's
-- address as it stands now — the best record there is, and a link lives
-- seven days.

ALTER TABLE email_verifications ADD COLUMN IF NOT EXISTS sent_to TEXT;

UPDATE email_verifications v SET sent_to = u.email
  FROM users u
 WHERE u.id = v.user_id AND v.sent_to IS NULL;

ALTER TABLE email_verifications ALTER COLUMN sent_to SET NOT NULL;
