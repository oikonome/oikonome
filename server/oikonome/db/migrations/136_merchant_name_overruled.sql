-- The aggregator's merchant name for one row, set aside.
--
-- A row's identity key is its outlet, else the merchant name the
-- aggregator gave it, else the bank's own descriptor. When a bank line and
-- the aggregator have agreed on one payee several times over and the
-- aggregator then calls a single charge on that line something the line
-- does not say, the resolver files that charge under the merchant the line
-- means. This column records the decision ON THE ROW: the name is set
-- aside, so the row keys on the bank line from then on, like a row the
-- aggregator never named.
--
-- Storing it is what keeps "one merchant per identity key" true. Left on
-- its aggregator name, the moved row would answer to a key that points at
-- a different merchant, and a rename of either side would drag the other's
-- rows along.
--
-- A re-sync that restates either string clears it, because the decision
-- was about the strings as they then read.
ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS merchant_name_overruled BOOLEAN NOT NULL
    DEFAULT false;
