-- Re-key cached LLM category rules to the CANONICAL merchant. apply()
-- resolves rule keys through merchant_canonical at query time, but llm
-- rules written before that (and their raw-variant duplicates) still sit
-- under raw descriptors ("Sq *Cedar Coffee Roast"), so one business can
-- carry N rule rows. That is dead weight for the Rules page (N rows shown
-- for one merchant) and permanently blocks the generic-pass unanimity gate
-- whenever two variants got different LLM guesses.
--
-- Deterministic merge rule — STRICTLY behavior-preserving, no winners
-- invented:
--   * A "group" is every rule row (any source) resolving to one canonical.
--   * Merge ONLY when the whole group is unanimous on category AND on the
--     disabled flag — exactly the condition under which apply() already
--     treats the group as one rule (COUNT(DISTINCT)=1), so collapsing it
--     cannot change any transaction's category.
--   * A group that disagrees is left ENTIRELY alone: conflicted canonicals
--     stay SPLIT — the user-first tiebreak never fires there, and
--     classified_at cannot discriminate either (variants classified in one
--     LLM batch share a timestamp to the hour).
--   * Only source='llm' raw rows are deleted/re-keyed; user and seed rows
--     are never touched (user rules already live at the canonical; seed
--     rows still resolve at query time).
--   * The surviving canonical row keeps source='llm' and the newest
--     classified_at of the merged rows; an existing row already AT the
--     canonical key (any source) is kept as-is and simply absorbs the raws.
--
-- Counts are reported (RAISE NOTICE) before anything is decided by a human
-- reading the deploy log; the statement itself is atomic. Runs cross-tenant
-- on the migration (admin) connection, so tenant_id is carried explicitly.

DO $$
DECLARE n_deleted integer; n_created integer; n_split integer;
BEGIN
  WITH resolved AS (
    SELECT m.tenant_id, m.merchant, m.source, m.category_primary,
           m.disabled, m.classified_at,
           COALESCE(mc.canonical, m.merchant) AS canon
    FROM merchant_categories m
    LEFT JOIN merchant_canonical mc
           ON mc.tenant_id = m.tenant_id AND mc.raw_merchant = m.merchant
  ),
  grp AS (
    -- only canonicals that actually carry raw-keyed llm rows
    SELECT tenant_id, canon,
           COUNT(*) FILTER (WHERE merchant = canon)   AS at_canon,
           COUNT(DISTINCT category_primary)           AS n_cats,
           COUNT(DISTINCT disabled)                   AS n_flags,
           MIN(category_primary)                      AS cat,
           BOOL_AND(disabled)                         AS all_disabled,
           MAX(classified_at) FILTER (WHERE source = 'llm') AS latest
    FROM resolved
    GROUP BY tenant_id, canon
    HAVING COUNT(*) FILTER (WHERE merchant <> canon AND source = 'llm') > 0
  ),
  mergeable AS (
    SELECT * FROM grp WHERE n_cats = 1 AND n_flags = 1
  ),
  del AS (
    DELETE FROM merchant_categories m
    USING merchant_canonical mc, mergeable g
    WHERE mc.tenant_id = m.tenant_id
      AND mc.raw_merchant = m.merchant
      AND m.merchant <> mc.canonical
      AND g.tenant_id = m.tenant_id
      AND g.canon = mc.canonical
      AND m.source = 'llm'
    RETURNING 1
  ),
  ins AS (
    INSERT INTO merchant_categories
           (tenant_id, merchant, category_primary, source, classified_at,
            disabled)
    SELECT tenant_id, canon, cat, 'llm', latest, all_disabled
    FROM mergeable
    WHERE at_canon = 0
    ON CONFLICT (tenant_id, merchant) DO NOTHING
    RETURNING 1
  )
  SELECT (SELECT COUNT(*) FROM del),
         (SELECT COUNT(*) FROM ins),
         (SELECT COUNT(*) FROM grp) - (SELECT COUNT(*) FROM mergeable)
    INTO n_deleted, n_created, n_split;
  RAISE NOTICE ' re-key: % raw llm rule(s) merged (% new canonical row(s)); % conflicted canonical(s) left split',
      n_deleted, n_created, n_split;
END $$;
