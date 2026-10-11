-- An imported row that a bank feed set its own copy of a charge aside for
-- now carries only a note of that fact (raw._stood_in: the feed's source
-- and account ids; see sync/base.py STOOD_IN_KEY), and undoing the import
-- or deleting its account keeps such a row.
--
-- An earlier format stored a full copy of each feed row instead, under
-- raw._covers. This turns every stored copy into the note it implies and
-- drops the copies, so no feed row (a hidden account's included) lives on
-- inside an imported row. Keep in step with
-- base.stood_in_from_legacy_copies, which does the same for an archive
-- written then. Re-runnable: a converted row has no _covers left.

UPDATE transactions t
   SET raw = (t.raw - '_covers') || jsonb_build_object(
         '_stood_in', jsonb_build_object(
           'source', COALESCE(
               CASE WHEN jsonb_typeof(t.raw->'_stood_in'->'source') = 'string'
                    THEN t.raw->'_stood_in'->>'source' END, 'feed'),
           'accounts', COALESCE((
               SELECT jsonb_agg(DISTINCT a ORDER BY a)
                 FROM (SELECT e.value->>'account_id' AS a
                         FROM jsonb_each(t.raw->'_covers') e
                        WHERE jsonb_typeof(e.value) = 'object'
                          AND jsonb_typeof(e.value->'account_id') = 'string'
                          AND e.value->>'account_id' <> ''
                       UNION
                       SELECT jsonb_array_elements_text(
                                t.raw->'_stood_in'->'accounts')
                        WHERE jsonb_typeof(t.raw->'_stood_in'->'accounts')
                              = 'array') u), '[]'::jsonb)))
 WHERE jsonb_typeof(t.raw->'_covers') = 'object'
   AND t.raw->'_covers' <> '{}'::jsonb;

-- an empty or malformed leftover carries nothing worth noting
UPDATE transactions
   SET raw = raw - '_covers'
 WHERE jsonb_typeof(raw) = 'object' AND raw ? '_covers';
