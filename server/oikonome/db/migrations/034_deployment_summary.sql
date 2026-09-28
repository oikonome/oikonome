-- Admin-console Deployments pane: alongside the version, show a one-line
-- "what changed" (the tip commit subject captured by install.sh at deploy
-- time and stamped by the migrate step). Nullable — older rows and any
-- deploy without the note just show "—".
ALTER TABLE deployments ADD COLUMN IF NOT EXISTS summary TEXT;
