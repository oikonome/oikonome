-- A proven pump is filed as fuel.
--
-- 093 identifies a chain's fuel arm from the bank's own statement line.
-- Where that line says the pump and the aggregator says something else, the
-- line wins: a grocery chain's fuel-station line can arrive under
-- FOOD_AND_DRINK_GROCERIES, which puts a tank of petrol inside a food
-- budget, and a superstore's fuel row under GENERAL_MERCHANDISE_SUPERSTORES.
-- sync/base.py files these as fuel on ingest; this does the same to what is
-- already stored.
--
-- Scope is exactly the rows the fuel rule proved and nothing else:
--   * merchant_outlet IS NOT NULL — the statement line named this brand and
--     then the fuel word. A row is never refiled because something else
--     classified it as fuel;
--   * category_override IS NULL — the user's own word is sacred and is not
--     read, compared or written here;
--   * rows already filed under TRANSPORTATION are left alone, so this
--     rewrites nothing that was already right.
--
-- This does move historical spending between categories, which moves past
-- budgets and reports. That is the point: the money was always fuel, and
-- the only thing changing is which bucket the app admits it was in.
UPDATE transactions
   SET category_primary = 'TRANSPORTATION',
       category_detailed = 'TRANSPORTATION_GAS'
 WHERE removed = 0
   AND merchant_outlet IS NOT NULL
   AND category_override IS NULL
   AND COALESCE(category_primary, '') <> 'TRANSPORTATION';
