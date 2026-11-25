-- A payer's name a person typed is kept as typed: income_documents.
-- payer_named_by_person, false for every name a reader stored. The list
-- reads a stored payer as an IRS transcript prints it (a parenthesised
-- part is the real name, a few capitals are the name control, another
-- form under the same EIN may carry the longer name), and a renamed payer
-- read that way was shown as something the person never typed. A marked
-- name is shown whole, never as an abbreviation, and is the name the
-- other forms under its EIN are shown by (taxdocs.payer_shown).
--
-- A constant default adds the column without rewriting the table. A build
-- older than this one never names it: its renames are not marked, and
-- every form it stores starts unmarked, as it should.

ALTER TABLE income_documents
    ADD COLUMN IF NOT EXISTS payer_named_by_person BOOLEAN NOT NULL
        DEFAULT false;
