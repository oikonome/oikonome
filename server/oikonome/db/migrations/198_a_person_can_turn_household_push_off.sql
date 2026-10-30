-- A household's push notices (the daily verdict, alerts, reports) reach
-- every member's browsers and phones. Email already has a per-person
-- switch — the address in the household's mute list — and push had none:
-- the only way for one person to stop them was to unregister every
-- device. This is that switch, per login: set by the person themselves
-- (every role) or by the owner for anyone in the household. Notices about
-- the person's OWN account (a security notice) still reach them.
ALTER TABLE users ADD COLUMN IF NOT EXISTS push_muted BOOLEAN NOT NULL DEFAULT FALSE;
