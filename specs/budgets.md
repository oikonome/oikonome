# Custom budget buckets (spec)

Decisions:
1. STRUCTURE: Food + Everything-else remain the fixed defaults. Users may
   add CUSTOM buckets that carve spend OUT of a named parent (either Food
   or Everything-else). Totals always reconcile: parent actual = parent
   raw spend − sum(child bucket actuals); parent budget is what the user
   set minus nothing (children have their own budgets; the parent's
   remaining budget is its own number — display both).
2. MAPPING: a bucket = set of categories (effective category, existing
   COALESCE semantics) + optional merchant overrides (canonical merchant
   match wins over category). Merchant rule assigns the txn to the bucket
   regardless of category.
3. AUTO-BUDGET: "Suggest from history" button — trailing 6-month median
   per bucket (exclude the current month; exclude outlier months >2× the
   median); user approves each suggestion; numbers then stay fixed until
   re-run. Applies to Food/Other too.
4. VERDICT MATH UNCHANGED: verdict = total variable actual vs total
   variable budget exactly as today; buckets are the WHY (bars, email
   sections, reasons). Variable-only rule, spend exclusions, envelope
   bills, tolerance — all untouched.
5. CARVE SCOPE: any bucket names its parent (food | other).
6. UI: Settings → Budgets card grows: bucket list (name, parent, budget,
   category picker, merchant list), add/edit/delete, Suggest button.
   Today bars + email show custom buckets indented under their parent
   (legacy bar style). Config keys: `custom_buckets` list of
   {name, parent, monthly, categories[], merchants[]}.

Constraints: the existing verdict semantics are frozen; every existing test must
stay green; new engine work in budget.py behind the existing month_status
surface; email renders from the same single source (todayview).
