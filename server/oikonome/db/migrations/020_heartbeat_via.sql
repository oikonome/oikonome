-- record HOW a collector pushes (script token over HTTP,
-- container-exec bridge, in-app upload) so Doctor can say the mechanism
-- next to each heartbeat. (Mirrored in schema.sql.)
ALTER TABLE script_heartbeats ADD COLUMN IF NOT EXISTS via TEXT;
