-- A hand-entered asset (tenant_settings.config -> manual_assets) now keeps
-- the day it entered the household, `acquired_on`, apart from `as_of`,
-- the day its value was last stated. The net-worth trend counts an item
-- from its acquired_on month; as_of is restamped by every value edit, so
-- reading it instead dropped a revalued house out of every earlier month.
--
-- A row stored before the field existed gets its as_of — the best date
-- there is for it. A row that already has acquired_on, or whose as_of is
-- not a date, is not written, so a second run changes nothing. The
-- restore applies the same rule to an archived row
-- (restore.clean_manual_assets).

UPDATE tenant_settings t
   SET config = jsonb_set(t.config, '{manual_assets}', (
       SELECT jsonb_agg(
                CASE WHEN jsonb_typeof(e) = 'object'
                      AND NOT e ? 'acquired_on'
                      AND jsonb_typeof(e->'as_of') = 'string'
                      AND e->>'as_of' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                     THEN e || jsonb_build_object('acquired_on', e->'as_of')
                     ELSE e END
                ORDER BY n)
         FROM jsonb_array_elements(t.config->'manual_assets')
              WITH ORDINALITY AS x(e, n)))
 WHERE jsonb_typeof(t.config->'manual_assets') = 'array'
   AND EXISTS (
       SELECT 1 FROM jsonb_array_elements(t.config->'manual_assets') e
        WHERE jsonb_typeof(e) = 'object'
          AND NOT e ? 'acquired_on'
          AND jsonb_typeof(e->'as_of') = 'string'
          AND e->>'as_of' ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$');
