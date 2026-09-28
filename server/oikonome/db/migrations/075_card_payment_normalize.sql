-- Money-in rows on a credit card that describe a payment ARE card payments,
-- whatever the aggregator called them.
--
-- Plaid can send a card settlement ("Payment Thank You - Web") as
-- LOAN_DISBURSEMENTS_OTHER_DISBURSEMENT while its sibling card payments
-- arrive correctly as LOAN_PAYMENTS_CREDIT_CARD_PAYMENT. Only the latter is
-- in the engine's spend exclusion list, so a mislabelled row counts as real
-- money in — and the ledger draws it green beside identical grey ones.
--
-- sync/base.py normalizes this on ingest for every aggregator; this catches
-- what is already stored. Deliberately narrow:
--   * credit accounts only (the card context is what makes a bare "payment"
--     word trustworthy — on checking it could be any merchant),
--   * money IN only (amount < 0 in engine sign),
--   * a payment word in the description, so a genuine merchant refund on the
--     card keeps being a refund,
--   * category_override untouched — the user's word is never overwritten.
UPDATE transactions t
   SET category_primary  = 'LOAN_PAYMENTS',
       category_detailed = 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
  FROM accounts a
 WHERE a.tenant_id = t.tenant_id
   AND a.id = t.account_id
   AND a.type = 'credit'
   AND t.removed = 0
   AND t.amount < 0
   AND COALESCE(t.category_detailed, '') <> 'LOAN_PAYMENTS_CREDIT_CARD_PAYMENT'
   AND COALESCE(t.merchant_name, t.name) ~*
       '(payment|pymt|pmt|autopay|directpay|thank you)';
