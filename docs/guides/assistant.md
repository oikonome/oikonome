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
  "what did I spend on medical this year". It also returns the most
  recent 24 months of that filter month by month, so "by month this
  year" is one lookup. Same personal-spend
  predicate as the Cash Flow page's Spending tab (no transfers, no
  card payments), so the number is the page's number.
- **recurring bills** — your bills with their amounts and health, from
  the same analysis behind the Bills page (see the Bills guide).

Those four summaries are the **only** data the model can reach. It
picks which to fetch, over up to three rounds of tool calls per
question — at most four fetches in a round, each result kept under
4,000 characters before the model reads it (a long list is shortened to
its most recent months or its top entries rather than cut off) — and
writes a short answer with the actual figures. It never sees free-form SQL and never receives
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
  endpoint, local data; remote endpoint, your call. Anthropic's
  endpoint (`https://api.anthropic.com`, one of its model ids, an
  Anthropic API key) needs no extra settings; the app sends it only the
  fields it accepts.

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

The page is a chat. On the left (on a phone: the first screen) is the
list of **conversations**, newest first; **+ New conversation** opens a
fresh one, with example questions as one-tap chips. Type a question
into the box at the bottom and press **Enter** or **Send**
(Shift+Enter for a new line). Your question appears as a message, the
answer under it, and the box now reads **Continue the conversation…**:
anything you type next continues that same conversation, and the model
is handed its earlier questions and answers — and nothing from any
other — so "and last year?" means what you meant. A follow-up sees the
newest eight turns of its conversation, each answer trimmed to a
readable length; charts are not re-read, the prose is.

Under each answer a small **from net_worth · spending_summary** line
names the summaries that fed it, and **copy** puts the text on the
clipboard (without the bold markers). The conversation's header has
**save as report** and **remove**. A question that fails stays in the
conversation with **try again** until you dismiss it. Each conversation
is a link (`?c=` in the address bar), so the browser's back button
leaves it; on the phone the device's back button does.

If someone removes a conversation while you are continuing it, your
question is asked again as a new one, and its answer carries a note:
"the earlier conversation was removed, so this was asked as a new
question". If the conversation is removed while your question is being
answered, the answer is still shown, on that screen only, with the note
"the conversation was removed while this was answered, so this answer was
not kept". An answer the app could not keep in the history (it was
answered, but saving it failed) is still shown, on that screen only, and
cannot be continued — its box opens a new conversation; a follow-up that
could not be kept stays in its conversation, which you can go on
continuing.

Answers are written in plain prose; the model may bold a key figure or
use a short bullet list, and the page draws those as bold and bullets
(so does the mobile app and the saved report).

## History

Every conversation is kept on your instance, with its answers, their
charts and the time each was asked, and listed newest-first, each one's
questions in the order they were asked — on the web and in the mobile
app alike, so a chart drawn on the phone is on the web too. The log is the **household's**: everyone
who can sign in reads the same one, each entry naming who asked. When a
member is removed, their questions stay but lose their name. The shared
demonstration login sees its own questions under its address and
everyone else's as "a household member". A question that failed (a
model timeout, a busy instance) is not kept.

**remove** in an open conversation's header drops it — every turn of
it; **clear all** under the list drops every conversation. Both are edits, so a view-only login
reads the log but cannot tidy it. The log keeps about each person's
newest 200 answered questions; as they ask new ones, their own questions
in their least recently active conversations go. A conversation never
loses its opening question while any follow-up of it stays — in a long
conversation the oldest follow-ups go first. Asking only ever removes the
asker's own questions, so one person asking a lot never pushes out anyone
else's. **older
conversations** at the bottom pages back through what is kept, without
skipping or repeating an entry. The history pages by question, not by
conversation, so a long conversation can begin on an older page; it then
shows "continues a conversation that started earlier — “older
conversations” loads its start". When there is no older page left to
load and the start still is not there (an older version of the app could
remove single answers), it shows "the start of this conversation is no
longer in the log" instead.

The log stays on the instance: it is **not part of the data export**
(the figures in it are the ledger's own, which does travel) and it is
not mirrored anywhere.

## Charts, tables and reports

When a picture or a table makes an answer clearer — "chart my groceries
by month", "compare 2024 and 2025 spending", "where does my money go" —
the answer can carry one, drawn in place on the web and in the mobile
app: a **bar** chart, a **line** chart, a **pie** (parts of a whole) or a
small **table**. Under every chart, **show numbers** folds out the same
figures as a table, so the picture is never the only way to read a
number.

The model never writes markup. It describes the chart as a small list of
labels and values taken from the summaries it fetched; the server checks
that description against a strict shape (a handful of chart types, a
bounded number of points, numbers where numbers belong) and never draws
anything that doesn't fit. What you see is drawn by the app, from
figures the engine produced.

**save as report** (in an open conversation's header) downloads the
whole conversation — every question, its answer's prose, the charts,
every chart's figures as a table, in order — as one self-contained HTML
file: inline graphics and styles, no scripts, no links out, so it opens
the same from a Downloads folder, a mail attachment or a phone's share
sheet. On mobile it goes straight to the share sheet. The file is built
on the spot from the conversation as it was kept in the history. A very
long conversation keeps its newest fifty questions (fewer when they
carry many charts), and the page says how many earlier ones it left
out; a conversation whose start is not loaded says so too.

The running app links to its source code in the footer (**source**),
as free software should.

## Gotchas

- **Four summaries is the whole toolbox.** Questions outside them —
  a specific transaction, a single day, a forecast — get an honest
  "the tools don't cover that" rather than an invented number. Use the
  pages for anything precise. A chart can only show what a summary
  returned: by-month figures come from a spend lookup, by-category
  figures from the spending summary.
- A chart the model describes badly (a value missing, a label that is
  not text), or one past the four an answer can carry, is not drawn:
  the answer says "(A chart for this answer could not be drawn.)" in its
  place. Ask again if the picture matters.
- The model phrases; the engine computes. If an answer's *number*
  looks wrong, check the corresponding page — the figure is the
  page's figure. If the *phrasing* misreads the data, ask again or
  rephrase; small local models occasionally fumble the wording.
- Answers on the bundled model can take a while on CPU — each model
  call is allowed up to 150 seconds before timing out, and a question
  takes at most five calls.
- If the model is still fetching after three rounds of tool calls, it
  gets one more turn with the tools withheld and answers from what it
  already fetched. If that turn comes back empty it is asked once more;
  only then does the answer read "I wasn't able to finish answering
  that — try rephrasing."
- Asks are rate-limited to **30 per hour** per client IP address.
- Only **three** questions are answered **at once** across the whole
  instance — each one holds a worker thread for as long as the model
  takes. An ask that arrives when they're all busy is refused right away
  (better than queueing behind a slow answer); retry in a moment.
  Operators can raise `OIKONOME_ASSISTANT_CONCURRENCY` on a big machine.
- Questions longer than 1,000 characters are truncated.
