# Assistant

The Assistant page answers one kind of question: **plain-language
questions about your own ledger** — "what's my net worth?", "how much
did I spend by category?", "what are my recurring bills?"

## How an answer is made

The model **routes and phrases — it never does the math**. Every figure
in an answer comes from the same engine functions that power the pages:

- **net worth** — the Net Worth page's computation: the liquid +
  investments total, the total including property, and a breakdown by
  asset class (see the Net Worth guide).
- **spending summary** — trailing-12-month totals by category, a
  year-by-category table, top merchants, and per-year totals (the same
  reporting the Cash Flow page's Spending tab draws on; see the Cash
  Flow guide).
- **spend lookup** — spend filtered by a merchant name, a category, a
  calendar year, or any combination: "how much at Target in 2025",
  "what did I spend on medical this year". Same personal-spend
  predicate as the Cash Flow page's Spending tab (no transfers, no
  card payments), so the number is the page's number.
- **recurring bills** — your bills with their amounts and health, from
  the same analysis behind the Bills page (see the Bills guide).

Those four summaries are the **only** data the model can reach. It
picks which to fetch, over up to three rounds of tool calls per
question — at most four fetches in a round, each result trimmed to
2,000 characters before the model reads it — and writes a short answer
with the actual figures. It never sees free-form SQL and never receives
your raw transaction rows — only these vetted, truncated summaries.

## Read-only, by construction

The assistant **cannot change anything**. There is no tool that
writes — no recategorizing, no editing bills, no settings changes.
Asking it to change something gets you a sentence, not a change.

Because it only reads, **everyone in the household can use it** — a
view-only login included. It answers from the same figures that login can
already see on the pages.

## Where your data goes

The assistant runs on whichever AI backend the **Assistant** task is
routed to (Settings → AI, the **AI (optional)** card). Several backends
can be configured at once — a local model doing the bulk categorization
while a cloud model answers here, or any other split:

- **Bundled model** (the standard install's default, or
  `./oikonome.sh llm on`): the model runs in a container on your own
  host. Your questions and the summaries never leave the instance.
- **Your own backends** (Settings → AI, or the `OIKONOME_LLM_*` env
  vars): questions and the fetched summaries go to whatever
  OpenAI-compatible server the Assistant task points at. Local
  endpoint, local data; remote endpoint, your call.

The Assistant page says which case you are in: a green **local** pill
("answered by the model on this server — read-only tools only, and
nothing leaves the box") when the backend is the bundled model or runs
on the same host, otherwise a line naming where your questions and the
figures they return go ("… go to *host*").

In hosted mode, a tenant-configured endpoint URL is re-checked against the
network guard at every use.

## No LLM, no page

When no LLM backend is configured, the **Assistant tab disappears from
the navigation entirely** — the feature is absent, not broken.
Configure a backend (or run the bundled model) and it appears.

## Asking

Type a question and press **Enter** or **Ask**. Example questions sit
under the box as one-click chips and stay there after every question,
so a second one is one click away. Answers stack newest-first below the
input; under each answer a small **from net_worth · spending_summary**
line names the summaries that fed it, and **copy** puts the text on the
clipboard while **ask a follow-up** puts the same question back in the
box to edit.

Each question stands alone — **the assistant has no memory of earlier
turns**, so "and last year?" won't work; ask the full question. The
conversation log lives only in your browser tab and clears on reload.

## Gotchas

- **Four summaries is the whole toolbox.** Questions outside them —
  a specific transaction, a month range, a forecast — get an honest
  "the tools don't cover that" rather than an invented number. Use the
  pages for anything precise.
- The model phrases; the engine computes. If an answer's *number*
  looks wrong, check the corresponding page — the figure is the
  page's figure. If the *phrasing* misreads the data, ask again or
  rephrase; small local models occasionally fumble the wording.
- Answers on the bundled model can take a while on CPU — each model
  call is allowed up to 150 seconds before timing out, and a question
  takes at most three calls.
- If the model wanders (fetching tools without concluding), the
  attempt stops after three rounds of tool calls without an answer,
  with "I wasn't able to finish answering that — try rephrasing."
- Asks are rate-limited to **30 per hour** per client IP address.
- Only **three** questions are answered **at once** across the whole
  instance — each one holds a worker thread for as long as the model
  takes. An ask that arrives when they're all busy is refused right away
  (better than queueing behind a slow answer); retry in a moment.
  Operators can raise `OIKONOME_ASSISTANT_CONCURRENCY` on a big machine.
- Questions longer than 1,000 characters are truncated.
