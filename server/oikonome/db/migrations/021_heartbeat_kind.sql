-- the collection half's SOURCE KIND (api | scrape), sent by the
-- collector with its push — lets the Accounts page badge a script-fed
-- item as "Script · API" vs "Script · scrape". (Mirrored in schema.sql.)
ALTER TABLE script_heartbeats ADD COLUMN IF NOT EXISTS kind TEXT;
