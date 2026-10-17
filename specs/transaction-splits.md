# One charge, several categories (spec)

A hand split of a single ledger row into (category, amount) parts.

Decisions:

1. **The person writes the parts; they must add up.** ≥ 2 parts, each a
   spending category (Plaid spend primary or a custom name — never a
   flow category), each non-zero with the row's sign, distinct
   categories, summing to the row's amount to the cent. Validated in one
   place (`engine/splits.validate`) and refused with a sentence the
   client shows as-is. A person's split is never proportional or
   derived for them: the arithmetic of a hand split is their claim about
   their money. (A STORE's split is — decision 6 — and is marked as
   such, so the two are never confused.)
   Only a row the spend rollups read can be split — exactly
   `budget.SPEND_ONLY_SQL` (`splits.SPLITTABLE_SQL`), nothing stricter: a
   refund, transfer, card payment or loan-account row would carry parts
   no report counts, while a loan payment from checking, which Spending
   does count, splits like any charge. The ledger row carries the answer
   as `splittable` and both clients offer Split only when it is true;
   restore applies the same rule. The stale sweep drops a split whose
   amount moved or whose row stopped passing that same test (a person
   made it a transfer); a category the bill pass or the categorizer
   stamps is never judged on its own, so they still need know nothing of
   splits. The door reads the amount under a row lock in the same
   transaction as the write, and the sweep re-judges each split inside
   its DELETE, so neither a sync nor a re-split in flight is lost.
2. **The split is an overlay, not a rewrite.** `category_override` /
   `category_primary` are untouched, so sync, the bill pass, the store
   matchers and the LLM refiner need no knowledge of it; removing the
   split hands the row back whole. A row whose amount moves out from
   under its split (a pending charge posting for another total) drops
   the split on the next sync-time sweep (`splits.drop_stale`).
   A split follows the charge the way a note does: when its row is
   retired while the charge lives on under another id — a pending row
   settling, a reconnect twin retired, a re-numbered charge re-anchored —
   the parts move with it (`splits.move_split`). All or nothing, never
   onto a row that has parts of its own; a copy of the destination's
   split is dropped, a differing one stays on the retired row and is
   counted by the re-anchor sweep. A move onto a different amount is
   then dropped by the stale sweep, judged against the row that counts.
3. **Readers by category join; readers by row do not.** There is no
   per-row flag every reader must remember; a per-row flag is one reader
   away from being forgotten. A rollup BY CATEGORY adds `categories.SPLIT_JOIN` and reads
   `PART_CAT` / `part_net(...)`: a split row fans out into its parts, an
   unsplit row is its own single part, and every reader that joins
   agrees with every other. `budget._spend_rows` fans out by default
   (one row per part, same `txn_id`, `split_line` set), which is what
   carries the verdict, the buckets, the bill matcher, the Why, Today,
   the email and the month lens; `reporting.compute_spending`,
   `spending_window` and `lenses.year_summary` / `year_category_ledger`
   join in SQL. Readers that count whole rows — totals by month or
   merchant, `SPEND_WHERE` / `SPEND_ONLY_SQL`, the anomaly pass
   (`parts=False`), the ledger listing — never join. Flow categories are
   refused in parts precisely so the whole-row spend test and the
   per-part category test cannot disagree.
4. **Consequences accepted, and documented for the user:** a bill's
   matcher sees the parts as separate charges (a split bill payment
   matches only if a part carries the bill's amount); a partial
   reimbursement is shared pro rata across parts, rounded to the cent;
   `search_text` does not index part categories (it would need a table
   rewrite); business books (Schedule C classes) are
   per-row and unaffected.
5. **Surfaces.** The ledger row carries `split` (the parts, stored keys)
   and `split_source` in the three-way row contract; the category filter
   and the assistant's category search answer for any part. Web: ✂ in
   the row's ⋯ strip, a panel under the row (`SplitPanel`), the parts
   drawn in the category cell in place of the picker, the pill saying
   "split by receipt" for a store's parts. Mobile: a card on the
   transaction screen with the same editor, the category sheet serving
   one part at a time; a "✂ split N ways [by receipt]" mark on the
   ledger row. Export/restore round-trips the PERSON's
   `transaction_splits`; a store's are regenerated from the restored
   records.
6. **A store's records split the charge they explain
   (`transaction_splits.source` = 'amazon' / 'costco').** The budget's
   food bucket reads "Costco - Food & Drink" per PART. Without a split, a
   mixed receipt would sit whole in its dominant category, so the hardware
   half of a grocery run would count as food and the grocery half of a
   hardware run would not. `store_match.record_parts` buckets a matched record's items by
   category (Costco: the receipt's per-item department category and
   `amount`; Amazon: the per-item category the local model stamps on
   `items_json` and the item `price`), nets a storewide coupon pro rata,
   scales the buckets to the cents the card was charged with
   largest-remainder rounding, and writes them under the store's prefix
   when two or more remain — validated by the same `splits.validate` a
   person's parts pass. Recomputed from scratch with the match on every
   run (`DELETE … WHERE source = <store>`), never over a row that has a
   hand split or whose category the person pinned (`override_source =
   'user'`, the pins restated just before), never on money in. A charge
   that is one of several for one record (an order billed per shipment)
   takes the record's proportions scaled to its own amount — the record
   does not say which item shipped when. The person outranks the store
   the way they outrank every machine pass: PUT replaces the parts as
   theirs; DELETE on a store's parts also pins the row's category
   (`data.set_category`), so the next run reads the row as answered;
   clearing that pin hands the row back to the receipt.
