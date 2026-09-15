-- learned categorization rules are inspectable + controllable. A
-- rule the user disables must stop categorizing (but stay visible, so they
-- can re-enable it) rather than be deleted. Default false = every existing
-- rule stays active.
ALTER TABLE merchant_categories
    ADD COLUMN IF NOT EXISTS disabled BOOLEAN NOT NULL DEFAULT false;
