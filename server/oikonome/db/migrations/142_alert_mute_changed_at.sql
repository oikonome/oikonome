-- When an alert's mute last changed. The daily email carries a signed
-- "mute" link that stays valid for a week; without a time on the mute,
-- a link used once and then undone in the app (the alert restored) would
-- mute the alert again on every replay. The emailed link refuses when the
-- mute changed after the mail was sent.
--
-- A trigger rather than a stamp in each writer: the mute is flipped by the
-- dismiss and restore doors and reset inside the snapshot upsert when an
-- alert recurs, and a stamp any one of them forgot would re-open the
-- replay. NULL means the mute has not changed since this column arrived.

ALTER TABLE alerts_log ADD COLUMN IF NOT EXISTS mute_changed_at TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION alerts_log_stamp_mute() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.dismissed IS DISTINCT FROM OLD.dismissed THEN
        NEW.mute_changed_at := now();
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS alerts_log_stamp_mute ON alerts_log;
CREATE TRIGGER alerts_log_stamp_mute BEFORE UPDATE ON alerts_log
    FOR EACH ROW EXECUTE FUNCTION alerts_log_stamp_mute();
