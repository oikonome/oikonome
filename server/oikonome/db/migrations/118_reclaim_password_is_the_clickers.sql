-- The reclaim link must not carry a password.
--
-- The person who FILLED THE FORM and the person who CLICKS THE LINK are
-- different parties in exactly the case this feature exists for. With a
-- parked password, an attacker could park a signup for an address they do
-- not own, with a password they chose; the real mailbox owner would click
-- "finish", and the account — verified, theirs, freshly provisioned —
-- would carry the attacker's password.
--
-- So nothing about the credential survives the mail. The parked row
-- keeps only the intent (which address, which invite), and the confirm
-- page asks the clicker to set the password there.

ALTER TABLE signup_reclaims DROP COLUMN IF EXISTS password_hash;
