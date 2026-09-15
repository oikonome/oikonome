-- A bill can carry a TRANSACTION category applied to the rows it matches
-- (a storage bill at a moving-truck merchant → RENT_AND_UTILITIES, while
-- the merchant's other rows keep the merchant rule). Those overrides are
-- recorded in manual_categories with the bill that set them, so they can be
-- reverted when the bill's category changes and never mistaken for a
-- person's own correction (which always wins over the bill's).
ALTER TABLE manual_categories ADD COLUMN IF NOT EXISTS bill_id TEXT;
