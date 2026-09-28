-- A household has three roles: the owner, members who can edit the
-- money, and view-only readers. The column is free text with 'owner' as
-- its default, and with an owner picking between the three from a
-- dropdown, a typo or a restored row carrying something else
-- would land in the column and be read by the write gate.
--
-- The gate treats an unrecognised role as a viewer (least privilege), so
-- this constraint is not what makes a bad value safe; it is what stops one
-- from being STORED, which is the difference between a bug that shows up
-- as "why can't they edit anything" and one that never happens.
ALTER TABLE users
  DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users
  ADD CONSTRAINT users_role_check
  CHECK (role IN ('owner', 'member', 'viewer'));

-- The invite carries the role the claim will create, so it answers to the
-- same set — minus 'owner': there is exactly one, and transferring the
-- account is not something a share link should be able to do.
ALTER TABLE invites
  DROP CONSTRAINT IF EXISTS invites_role_check;
ALTER TABLE invites
  ADD CONSTRAINT invites_role_check
  CHECK (role IN ('member', 'viewer'));
