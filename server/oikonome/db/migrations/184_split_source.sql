-- Who wrote a split: the person ('user'), or a store's own purchase record
-- ('amazon', 'costco'). A store matcher now carves a matched charge into the
-- categories the receipt's items name — groceries $80 · home $20 — so a
-- mixed warehouse run counts in the right buckets instead of wholly in its
-- dominant one. Those parts are the record's reading, recomputed from
-- scratch on every match run, and a person's split must outrank them and
-- never be touched by that recompute; the column is how the two are told
-- apart. Every split written before this column is the person's.
ALTER TABLE transaction_splits
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'user';
