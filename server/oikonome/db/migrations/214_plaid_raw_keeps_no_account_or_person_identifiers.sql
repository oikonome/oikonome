-- A Plaid object stored raw keeps nothing that identifies an account or a
-- person: raw leaves the instance verbatim through the export and the
-- portability archive, and nothing the app computes reads these fields.
-- Migration 146 cut a loan's account_number; the payment reference number
-- a student loan carries beside it (normally the same loan number), a
-- mortgage's property address, a transaction's account owner and an
-- account's owners stayed. The Plaid pulls now drop all of them as they
-- write (sync/plaid.py PLAID_RAW_IDENTIFIERS, the one list, which the
-- export applies on the way out too); this drops them from the rows stored
-- earlier. Every ARRAY below is that list verbatim.
--
-- Idempotent: a key removed is not there to remove again, and only rows
-- that carry one are written.

UPDATE liabilities
   SET raw = raw - ARRAY['account_number', 'payment_reference_number',
                         'property_address', 'account_owner', 'owners']
 WHERE jsonb_typeof(raw) = 'object'
   AND raw ?| ARRAY['account_number', 'payment_reference_number',
                    'property_address', 'account_owner', 'owners'];

UPDATE accounts
   SET raw = raw - ARRAY['account_number', 'payment_reference_number',
                         'property_address', 'account_owner', 'owners']
 WHERE jsonb_typeof(raw) = 'object'
   AND raw ?| ARRAY['account_number', 'payment_reference_number',
                    'property_address', 'account_owner', 'owners'];

UPDATE holdings
   SET raw = raw - ARRAY['account_number', 'payment_reference_number',
                         'property_address', 'account_owner', 'owners']
 WHERE jsonb_typeof(raw) = 'object'
   AND raw ?| ARRAY['account_number', 'payment_reference_number',
                    'property_address', 'account_owner', 'owners'];

UPDATE transactions
   SET raw = raw - ARRAY['account_number', 'payment_reference_number',
                         'property_address', 'account_owner', 'owners']
 WHERE jsonb_typeof(raw) = 'object'
   AND raw ?| ARRAY['account_number', 'payment_reference_number',
                    'property_address', 'account_owner', 'owners'];
