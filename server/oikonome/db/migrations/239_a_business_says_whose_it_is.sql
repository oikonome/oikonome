-- Whose a business is: the primary earner's, the spouse's, or not said.
-- A W-2 says whose it is and a business did not, so the self-employment
-- tax could not tell whose W-2 Social Security wages close the
-- business's wage base (specs/business-books.md, "Whose business it
-- is"). 'unknown' nets no wages: the estimate may read high, never low.
-- Every business that existed before this reads 'unknown' until its owner
-- sets it. A constant default: no table rewrite (the check reads the few business
-- rows once), no index. Re-runnable.
-- Mirrored in schema.sql.

ALTER TABLE business_entity
    ADD COLUMN IF NOT EXISTS owner TEXT NOT NULL DEFAULT 'unknown'
        CONSTRAINT business_entity_owner_is_known
            CHECK (owner IN ('primary', 'spouse', 'unknown'));
