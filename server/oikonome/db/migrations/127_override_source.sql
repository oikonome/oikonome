-- Several writers set category_override: a person's pin (manual_categories
-- with bill_id NULL), a bill's stamp (manual_categories with bill_id), and
-- a store item match ("<Store> - …", no manual_categories row at all).
--
-- The kind of an override is a fact on the row. Every writer states its
-- own kind and refuses to overwrite a kind that outranks it (a person > an
-- item match > a bill), so nothing depends on write order. Existing rows
-- are classified from the evidence on them: the manual_categories row,
-- else the store prefix, else a person.
ALTER TABLE transactions ADD COLUMN IF NOT EXISTS override_source text;

UPDATE transactions t
   SET override_source = CASE
         WHEN m.transaction_id IS NOT NULL AND m.bill_id IS NULL THEN 'user'
         WHEN m.transaction_id IS NOT NULL THEN 'bill'
         WHEN t.category_override LIKE 'Amazon - %' THEN 'amazon'
         WHEN t.category_override LIKE 'Costco - %' THEN 'costco'
         ELSE 'user' END
  FROM transactions t2
  LEFT JOIN manual_categories m
         ON m.tenant_id = t2.tenant_id AND m.transaction_id = t2.id
 WHERE t.tenant_id = t2.tenant_id AND t.id = t2.id
   AND t.category_override IS NOT NULL AND t.override_source IS NULL;
