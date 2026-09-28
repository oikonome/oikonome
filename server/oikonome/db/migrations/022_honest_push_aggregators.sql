-- Push-door items carry their real aggregator (coinbase / plan_csv), not
-- 'csv', so everything that keys on the aggregator (the namespace refusal,
-- the token-import guard, doctor grouping) sees their origin. Re-stamp
-- push-door rows still marked 'csv'.
--
-- Scope is exact: only the push doors ever created 'csv' items in these
-- namespaces (the coinbase door enforces its item namespace server-side;
-- the plan-CSV door marks its item's access_token '<provider>:scrape').
-- Native CDP-linked
-- coinbase items already carry aggregator='coinbase' and are untouched.
-- status: 'archived' keeps push-fed items out of the hourly pull loop and
-- the stale-connection checks (script heartbeats own their freshness).

UPDATE items SET aggregator = 'coinbase', status = 'archived'
 WHERE aggregator = 'csv' AND id LIKE 'coinbase-%';

UPDATE items SET aggregator = 'plan_csv', status = 'archived'
 WHERE aggregator = 'csv' AND access_token LIKE '%:scrape';
