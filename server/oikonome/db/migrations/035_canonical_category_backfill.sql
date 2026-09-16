-- One-time backfill of USER category rules from existing
-- per-transaction overrides, keyed to the CANONICAL merchant.
--
-- Why it is needed: an import path that writes category_override +
-- manual_categories rows DIRECTLY bypasses the set_category() branch that
-- teaches merchant_categories. Merchants corrected that way have NO user
-- rule, and apply()'s generic pass only overwrites vague buckets — a sharp
-- wrong aggregator category is never repaired. This derives the missing user rule from the corrections the user
-- already made, so apply()'s USER pass can repair those rows going forward.
--
-- Safety: UNANIMITY-GATED — a rule is derived for a canonical merchant only
-- when EVERY per-transaction override under that canonical agrees. A canonical whose
-- overrides disagree is left alone.
-- Flow categories (spend exclusions) are excluded on both the override and
-- the current category. Never clobbers an existing USER rule; only upgrades a
-- non-user (llm/seed) rule sitting at the canonical key. No-op for tenants
-- with no such overrides. Runs cross-tenant (admin/migration conn,
-- RLS bypassed) so tenant_id is grouped + written explicitly.

DO $$
DECLARE n integer;
BEGIN
  WITH agreed AS (
    SELECT t.tenant_id,
           COALESCE(mc.canonical, t.merchant_name, t.name) AS canon,
           MAX(t.category_override) AS cat
    FROM transactions t
    LEFT JOIN merchant_canonical mc
           ON mc.tenant_id = t.tenant_id
          AND mc.raw_merchant = COALESCE(t.merchant_name, t.name)
    WHERE t.removed = 0
      AND t.category_override IS NOT NULL
      AND t.category_override <> ''
      -- FLOW-GUARD: never derive a spend rule from a flow correction
      AND t.category_override NOT IN
          ('TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS','INCOME','BANK_FEES')
      AND COALESCE(t.category_primary,'') NOT IN
          ('TRANSFER_IN','TRANSFER_OUT','LOAN_PAYMENTS','INCOME')
    GROUP BY t.tenant_id, COALESCE(mc.canonical, t.merchant_name, t.name)
    HAVING COUNT(DISTINCT t.category_override) = 1        -- unanimity gate
  )
  INSERT INTO merchant_categories
         (tenant_id, merchant, category_primary, source, classified_at)
  SELECT tenant_id, canon, cat, 'user', now()
  FROM agreed
  WHERE canon IS NOT NULL AND canon <> ''
  ON CONFLICT (tenant_id, merchant) DO UPDATE
     SET category_primary = EXCLUDED.category_primary,
         source = 'user', classified_at = now()
     WHERE merchant_categories.source <> 'user';   -- never clobber a user rule
  GET DIAGNOSTICS n = ROW_COUNT;
  RAISE NOTICE ' backfill: % user category rule(s) derived from unanimous per-transaction overrides', n;
END $$;
