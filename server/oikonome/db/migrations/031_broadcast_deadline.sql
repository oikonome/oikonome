-- reboot broadcasts carry a DEADLINE — the SPA renders a live
-- countdown, flips to its reconnecting overlay when it passes, and the
-- worker clears expired reboot broadcasts on startup (i.e. after the
-- host is back) so nobody has to remember to clean up.
ALTER TABLE broadcast ADD COLUMN IF NOT EXISTS deadline TIMESTAMPTZ;
