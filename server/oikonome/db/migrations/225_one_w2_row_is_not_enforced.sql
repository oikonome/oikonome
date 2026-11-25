-- Every W-2 upload is kept as its own row; drop the unique index migration
-- 221 added, so a second upload of the same W-2 is not an error. 221's
-- columns are kept and not read.

DROP INDEX IF EXISTS uq_income_documents_w2;
