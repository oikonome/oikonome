# Receipts on transactions (spec)

The rules:

1. **Parsing = the tenant's configured LLM** (the Settings smart-
   categorization endpoint, vision-capable model required), extracting
   merchant, date, tax, tip, and line items from an attached image/PDF.
   **No LLM = attachment-only mode**: the receipt stores and displays;
   parsing runs later once an LLM is configured. No tesseract/OCR
   dependency in the image.
2. **Line items are stored + taggable** (business / personal / custom
   tags) under their transaction. **Expense report** = tag + month
   filter → CSV/printable summary with receipt images linked — the
   Schedule-C flow. Budget math is UNCHANGED: the transaction's total
   still rules the verdict; line items explain, never re-bucket.
   (Item-level bucket splitting rejected — rewires verdict math. What
   the product offers instead is a HAND split of the row, `specs/transaction-splits.md`:
   the person writes the parts, and they must add up.)
3. **Images live in Postgres** (bytea, 5 MB cap per receipt) — one
   backup story: pg_dump / oikonome.sh backup / the dump download all
   carry receipts automatically.

Sketch: receipts table (tenant, txn_id, image bytea, mime, parsed
JSONB, status uploaded|parsed|failed); receipt_items (receipt, line
no, description, qty, amount, tag); upload from the transaction detail
UI; nightly LLM pass parses 'uploaded' rows; /api/receipts endpoints +
report export.

## What a receipt's lines count as

The model reads a receipt's lines, and reads them imperfectly: a unit
price where the paper printed a line total, the card it was paid with as
a line, a fee with a minus sign, the same photo attached twice. The
lines are stored as read; what each one COUNTS as is decided at read time
by ONE function, `receipts.reconcile`, which the Receipts page
(`search_items`), the itemized report (`report`) and the receipt panel
(`for_txn`) all go through (`_counted_lines` for the first two). Nothing
is rewritten in the database, so nothing needs a migration or a restore
rule, and a better rule later re-reads every receipt at once.

- **Payment lines are not purchases.** A line that names how the
  receipt was paid (a card, "payment", tender, change, a total or tax
  listed as an item: `is_tender_line`) is not counted.
- **Signs.** The slip's net sign decides, never its smallest line's: the
  counted lines are read so they net positive — a slip netting negative is
  the model's sign, not a refund, and is flipped whole; a coupon beside its
  item stays negative. **A slip on a refund counts negative**: those lines
  are then negated, however the slip printed them, so an item bought and
  returned nets to nothing on the Receipts page and on a tag report.
- **Quantity.** Where the lines × their quantities, plus tax and tip,
  match the printed total (or the charge) and the lines as read do not,
  each amount was a unit price and the line counts amount × quantity.
  Otherwise the amount is the line's total, whatever the quantity says.
- **One slip, one count.** Two receipts that are the same slip (below)
  count the lines of one of them; the panel lists the other's lines as
  counted on that copy (`duplicate_of`).
- **Do the lines add up?** The charge (the printed total while the
  receipt waits) less the counted lines, tax and tip is `lines_gap` on the
  receipt in the panel when it is more than a few cents ($0.05, or 0.5%),
  taken in the size of the money (on a refund too, so positive always
  means the lines come to less);
  the panel says "the lines come to $x less (more) than the charge".

Clients show what the server counted: the Receipts page's row amount is
the line's total, the panel lists the counted lines. An older client
reading the same fields shows the corrected amounts and never sees a
payment line or a duplicate's lines.

**The store name is 80 characters at most** (`STORE_MAX` in
`engine/receipts.py`): a reading keeps no more, the details door refuses a
longer one typed, and both clients' `receiptDetailsDraft` check the same
bound. The store the form was filled with, saved back untouched, is kept
as stored (no `merchant` sent from the clients; the door treats one equal
to the stored store the same way), so a longer name read before the bound
never blocks a save.

## A receipt outlives the row it was put on

A receipt is never left where no screen shows it. A charge's row can be
retired under it (a hold posts under a new id, the feed withdraws a
row, an account is restated), and three things then hold, each decided in
one place:

- **The same charge under another id takes the receipt, with everything
  else on the row.** Where the feed names the hold a posted row replaced,
  the settlement carries it (`sync.base._settle_pendings`), whatever the
  posted amount; where it does not, `sync.reanchor.find_twin` decides
  whether one live row is the same charge, and the whole row moves or
  none of it.
- **No such row, after a wait: back to waiting.** A receipt the matcher
  paired, or a person paired off the waiting list, is released by every
  matcher pass (`receipt_match.release_stranded`; the hand-paired one is
  kept for the person). One attached from the charge itself is different:
  it shares its row with a note and a category that the settlement carry
  and the re-anchor move together, and a retired row often comes back —
  under its own id (the feed reports it again, a statement is restated)
  or as the posted charge a sync later. So it is released only by the
  nightly pass, after the re-anchor has run (`receipt_match
  .release_unplaced`), and only when (`_unplaced_rows`):
  - the row has been gone longer than `adopt.RECONCILE_DAYS`, the wait a
    withdrawn pair gets, counted from when it left the ledger
    (`transactions.removed_at`, below), and the receipt has been on it
    that long too. A retired row with no such time is never old enough;
  - the re-anchor's verdict is "no twin". Two look-alikes it would not
    choose between are left where they are, for the person, rather than
    handed to the matcher;
  - no other receipt of its slip (`receipt_identity`, below: the same
    image, or a person's "Same receipt") is on the ledger or waiting
    with lines: the slip is already counted there, and released it would
    only wait beside it. A copy not read yet counts nothing, so it holds
    nothing back. The backlog of undecided receipts is decided first,
    before the release takes the pairing lock and in a bounded number of
    passes; a row carrying a receipt still undecided is held a night;
  - it is not held back by a look-alike (below, "The one automatic
    case").
  Released, it waits like a fresh snap: the matcher's ordinary rules
  apply to a read one (an exact charge, or the same store's charge inside
  the tip band once the window has closed), and one nothing could read is
  on the waiting list with its candidates, where a person pairs it by
  hand. Being released is not a refusal. A hold a person retired by hand
  keeps its receipt, and a check image never waits.
- **The Receipts page lists a receipt's lines whatever became of its
  charge** (`receipts.search_items`), so they are findable while the row
  is gone. They are LISTED, not added up: a line whose charge is off the
  ledger (retired, by the feed or by hand) is left out of every group
  total, and the itemized expense report counts only charges on the
  ledger and receipts waiting for one.
- **A group says how many were bought and on how many receipts**
  (`receipts.search_items`): `bought` counts quantity (a line read as
  "4 ×" is four; a line with no quantity, or a weighed one that is not a
  whole number, is one) and `receipts` counts the distinct slips the
  group's lines are on; a returned line on a refund's slip takes its
  quantity back off `bought`. Web and phone print them as two columns,
  "bought" and "receipts"; `n` (lines) stays for older clients.

## A receipt is one slip

A person whose receipt was stranded often photographs the slip again
for the posted charge, attaches the same file twice, or splits one slip
over two cards. Whether two receipts are the same slip is decided at a
named moment and STORED, never re-derived by a reader, and it is decided
by exactly two rules. Neither reads where a receipt sits, what it was
read as, or when anything happened.

- **The fact**: every receipt is in one slip. `receipts.slip_id` is the
  slip's label (another receipt's id; NULL = a slip of its own), with
  `slip_source` (`image` or `person`), `slip_decided_at` (NULL = never
  decided) and `not_same_slip` (receipts a person said are other
  purchases). `image_sha256` is generated by the database from the
  image; adding it (migration 232) rewrites the whole receipts table,
  image bytes included, under an exclusive lock, about 0.6 s per 100 MB
  measured locally, so the upgrade's lock time grows with the table. The label is not a foreign key: deleting the receipt whose id it
  is leaves the rest of the slip together, and a label no other receipt
  shares is a slip of one.
- **The two rules**, in `receipt_identity`:
  1. **The same image** (equal `image_sha256`, same kind) is one slip,
     wherever each copy sits — the same file on two live charges is one
     slip paid with two cards, counted once — unless a person said
     otherwise. `decide`, under the pairing lock, idempotent, on upload.
  2. **A person's word is final, both ways.** "Same receipt" joins the
     two slips (every member becomes `person`, and an earlier "Two
     purchases" between them is withdrawn); "Two purchases" takes the
     receipt out of the other's slip, and every receipt of either slip
     remembers every receipt of the other, so no rule ever joins them
     again and no other copy of either is asked about. The answer is
     read at slip level everywhere (`refused`, `suspected`, `_merge`),
     whichever copy the person tapped, the label holder included. An
     answer is taken only about two receipts a person is asked about —
     one slip already, the same file, or read alike (a refusal does not
     unmake that) — and any other two are refused (400): joining them
     would hide a purchase, and a refusal between them is never asked
     for. That also bounds what `not_same_slip` can grow to.
- **Readings never join.** Two receipts read alike — store trimmed and
  case folded, printed ISO day, total to the cent, both read — that are
  not one slip are *suspected* (`suspected`, one read of the look-alikes
  and one of the refusals, linear in the receipts read alike). Both
  count, and the person is asked, on every panel that shows either copy
  (web and phone: the receipt panel and the waiting list), "Same
  receipt" or "Two purchases". An automatic join on a reading (by
  placement, by the timing of a release, or by the one charge a stranded
  copy could match) can hide a real purchase or depend on what the bank
  has sent so far, so readings are never joined automatically.
- **When it is decided**: on upload (`receipts.add`), and a person's
  answer is stored when given. Nothing else changes a slip: moving a
  receipt, retiring its row, releasing it or reading it again leaves its
  slip as it was, so there is no re-decision and no timestamp is
  compared. `slip_decided_at IS NULL` alone marks a receipt to decide:
  one stored before the columns, or inserted by a writer that does not
  decide (an older build, a writer that copies a row whole, a restore of an archive
  without the columns). `decide_pending` decides those oldest first
  (one bounded batch; a receipt no other has the image of is decided in
  one statement); `decide_backlog` repeats it a bounded number of times.
  The matcher pass runs one batch first, the nightly release the
  backlog before it takes the pairing lock, a restore the backlog
  once it has committed, and the worker's startup sweeps every
  household's backlog at boot (`worker.decide_receipt_backlogs`), so the
  receipts an upgrade left undecided are settled before the first page
  is read rather than at the next sync or nightly.
- **Counting reads the label** (`receipt_identity.slips`, used by the
  Receipts page, the expense report and the receipt panel). In each slip
  exactly one receipt is counted, by rank: on the ledger or waiting
  before a retired row; then one with lines; then, for a tag report, one
  carrying the tag; then a live charge before waiting; then the earliest.
  Every other copy's lines are listed with `counts: false` and
  `why: "same_slip"`; a line of a slip whose counted copy is on a
  retired row has `why: "off_ledger"` and is never added up. Deleting,
  unpairing or retiring the counted copy promotes the next.
- **The one automatic case: a hold-back, never a join**
  (`receipt_match._held_by_a_look_alike`). When the nightly would release
  a copy attached from a charge that left the ledger (above), it is left
  where it is, counting nothing as it did through the wait, if a receipt
  it carries reads exactly like a receipt on a LIVE charge of the same
  account that is not its slip and that no person has called another
  purchase, and the matcher has no other charge it could be (its
  candidates: in the amount band and window, carrying no receipt, not one
  a person took it off, not plainly another store). That is the stranded
  slip photographed again for the posted charge, whose tip put it beyond
  the re-anchor. Nothing is stored: it is judged again every night from
  the rows as they are, so a second same-price charge that posts later
  releases it the next night.
  - **What the person sees**: the live copy counts; its panel (and the
    held copy wherever it is listed) says *looks like the same receipt
    as the receipt on a charge no longer on the ledger … only this one is
    counted; say whether they are one purchase*, with **Same receipt** and
    **Two purchases**. The held copy's lines are listed on the Receipts
    page, greyed, *not counted: its charge is no longer on the ledger*.
  - **Both doors work on it**: "Same receipt" makes the two one slip; the
    slip rule above then keeps the held copy where it is, listed and not
    counted, and nothing is asked again. "Two purchases" sends the copy
    on the retired row back to waiting in the same step, under the
    pairing lock (`receipt_match.release_refused`): it counts, and the
    matcher or the person places it. That holds whatever kept it there —
    this hold-back, or a re-anchor that saw two charges it could be — and
    a refused pair is never held again. Left alone are a row a person
    retired by hand and a copy whose slip already counts elsewhere.
- **Where the total is read**: a counted line of a receipt that another
  COUNTED receipt reads exactly like (`receipt_identity.doubled`; on the
  expense report, a look-alike that carries the tag) carries
  `suspected: true` and `note` — the words *may be counted twice: …
  choose Same receipt or Two purchases*. A line that does not count
  carries its reason as `note` too (`LINE_SAME_SLIP`, `LINE_OFF_LEDGER`).
  A group of the Receipts page and the expense report carry `note` when
  their total adds up such a line. The server decides every word; web and
  phone print `note` as sent. Fields are only added.
- **The invariant** (`test_a_receipt_slip_is_decided_once`, a property
  test over generated orders of upload, read, settle, retire, pair and
  the nightly): a purchase counted twice is always offered to the person
  as a question; no purchase is hidden — on the ledger or waiting, or
  held back on a retired row after the nightly — without being put to the
  person beside a counted look-alike, identical bytes, or a person's
  answer; once every question is answered as the purchases really were,
  each counts exactly once, whatever the order of events.
- **What clients read back**: on the panel each receipt has
  `duplicate_of` (the counted copy, when this one is not counted; `items`
  is then empty and `listed_items` holds its lines), `counts`,
  `slip_twins` (the other copies, each with where it is and whether it
  counts) and `suspected` (receipts read alike that are not its slip,
  one per slip); the waiting list carries `counts`, `slip_twins` and
  `suspected`; the Receipts page rows carry `counts`, `why`, `suspected`
  and `note`, its groups and the expense report `note`. The answers are
  `POST /api/receipts/{id}/same-slip` and `/two-purchases` with
  `{"other": id}`, a member's or owner's door (a viewer is refused).
  Web and phone print the same prompt and the same buttons, and refetch
  the panel, the waiting list, the Receipts page and the expense report
  after an answer.
- **An older client** reads server-counted totals and sees the corrected
  figures; it lists a copy's lines on the Receipts page without the grey
  or the note, and shows no prompt. **A stale tab** that pairs, unpairs
  or deletes goes through the unchanged doors, and the slip is
  unaffected; an answer about a receipt that is gone is a 404.
- **Export / restore**: `slip_id`, `slip_source`, `slip_decided_at` and
  `not_same_slip` travel in `receipts.csv` (not the hash: the database
  makes it). Receipt ids are kept, so labels and answers land as
  written, and every restored receipt lands undecided: the restore ends
  with `decide_backlog`, which keeps those labels and answers and only
  adds the joins of the same file — an archive copy beside a local copy
  of it is one slip, and an archive without the columns gets its same
  files joined again and its look-alikes asked about again, never
  joined. An account re-keyed or adopted under a new id carries
  nothing of the slips: no slip column names an account or a
  transaction.

## When a row left the ledger

Every wait on a retired row — a receipt attached from the charge, a
reimbursement pair or a fronted business cost on a row the feed withdrew
— is counted from the moment the row left the ledger, never from the
charge's own date: an old charge a restated file or a full-replace push
retires today has its whole wait ahead of it.

- **The fact**: `transactions.removed_at`, when the row last went from
  live to retired; NULL while it is live. It says *when* only. *Why* stays
  where it was: `raw.feed_removed_at` marks a row the bank feed took back,
  `retired_at` a retirement someone decided.
- **The one writer** is a database trigger, `transactions_stamp_removed`
  (in `db/schema.sql`), not the doors: every path that retires a row does
  it with an UPDATE of `removed` (the feed's removal delta, the
  pending-to-posted settlement, the vanished-pending sweep, the twin
  dedup, a reconnect or re-link retiring holds, a QIF, plan-CSV or
  Coinbase restatement, a person retiring a stuck hold), and the trigger
  stamps the time when `removed` goes from 0 to non-zero. It also fires on
  INSERT: a row that arrives already retired (a restore, a writer that
  copies a row whole, a build that predates the column) is stamped as it lands, so no path
  leaves a retired row without a time. A row coming back live loses its
  time; retired again, it gets a new one and waits again. A row already
  retired keeps its time when a door retires it again. An explicit value
  in the same statement (a writer copying a row whole, its time included,
  an archive) is kept.
- **Readers**: `receipt_match._unplaced_rows` (the receipt release) and
  `budget.FEED_REMOVED_OLDER_SQL` (the withdrawn-pair and fronted-cost
  releases, `budget.release_withdrawn_pairs`, `equity
  .release_withdrawn_fronted`, which still ask `raw.feed_removed_at` for
  the kind of retirement). A retired row with no time (only an explicit
  NULL written onto an already-retired row can leave one) is treated as
  not old enough and named in the log, never as old.
- **Rows retired before the column existed** were stamped by migration
  230: the feed's own stamp where it reads as a time, else `retired_at`,
  else the migration's time — an unknown row waits one fresh window, so
  nothing is let go early.
- **Export / restore**: the column travels in `transactions.csv`. An
  archive without it (or with a value that is not a time) lands a retired
  row by the migration's rule, with the restore's own time last, so it
  waits a fresh window: its twin may arrive with the next sync (the
  trigger would give it the same time if the restore did not). A live
  row lands with none. An older build restoring a newer archive ignores
  the column. No client reads it; it is not in the API.
