-- store an OPTIMIZED (auto-cropped + touched-up) copy of a receipt
-- image ALONGSIDE the untouched original. `image` stays the pristine upload;
-- image_optimized is a cleaner, cropped, higher-contrast render for display
-- (best-effort — NULL when optimization was skipped or failed, so the UI
-- falls back to the original).
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS image_optimized BYTEA;
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS optimized_mime  TEXT;
