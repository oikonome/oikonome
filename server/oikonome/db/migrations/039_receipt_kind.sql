-- Check images: a receipt attachment can be a photo of a bank CHECK (front).
-- kind drives the vision prompt (check fields vs line items) and the panel
-- render; existing rows are ordinary receipts.
ALTER TABLE receipts ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'receipt';
