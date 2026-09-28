# Household roles: owner, member, viewer

A split between "the owner" and "everybody else, read-only" is the right
shape for an accountant or a curious teenager and the wrong shape for the
ordinary case this product is for: two adults running one set of books, where the
second adult is not a guest.

So there are three:

| role | sees | changes |
|---|---|---|
| **owner** | everything | everything, including the acts that end the account or hand it on |
| **member** | everything | the household's money — transactions, bills, budgets, rules, categories, receipts, imports, syncs, the business books |
| **viewer** | everything | nothing but their own account (password, factors, sessions, their own delivery preference) |

Everyone sees everything: a partner who can read the balances but
not the plan is a worse product, not a safer one.

**View-only remains the default.** An invite mints a viewer unless the
owner picks otherwise, and an owner who does not read the dropdown hands
out the smaller grant.

## Where the member line is drawn

Not at importance, and not at how much data an action touches.
Recategorising a year of transactions is a far bigger edit than
disconnecting a bank, and a member may do it: it happens inside the
household, everyone can see it, and it can be undone.

What a member may **not** do is the set where a mistake — or a person who
should not have been added — leaves the household's control:

- **Ending the account.** Deleting it, and whatever the account section
  of Settings holds on an instance an operator runs for other people.
- **Taking the data out.** The export ZIP, the database dump, the
  connections bundle — and restoring an archive over the household, which
  is the same door in reverse. A copy that has left cannot be recalled.
- **Deciding who else is in the household.** Invites, removals, roles,
  script tokens, support access. Without this the restriction is
  self-lifting: a member who can add a member can add an owner.
- **The bank connections.** Linking, re-keying, repairing, disconnecting,
  hiding or removing an account. These hold credentials, an aggregator
  charges per connected item, and disconnecting is felt by everyone in the
  house.
- **The mail plumbing and where data is sent.** SMTP, the daily-email
  recipient list, AI backend URLs. Pointing the household's mail at an
  outside mailbox is an export that arrives every morning, so it sits with
  the exports rather than with the settings a member may edit.

There is deliberately **no promotion to owner**. There is one owner;
transferring the account is a different act with different consequences
(the account's standing with its operator, deletion rights, the power to
remove the previous owner) and
should be designed on its own rather than fall out of a dropdown. The
picker offers member and view-only, and the invite table refuses
`owner` at the database.

## Where it is enforced

`server/oikonome/web/permissions.py` is the policy, and it is the only
place that states it:

- `can_write(role, path)` runs in the `current_user` choke point that
  every mutating request passes through. Owner: yes. Member: yes unless
  the path is owner-only. Viewer: only their own account. An unrecognised
  role is treated as a viewer — a value from a future version restored
  onto an older one must fail to the least privilege.
- `owner_only(path)` names the path families above, plus a couple of
  patterns for doors whose path carries an id.
- `read_only_route(path)` names the handful of doors that carry a body
  and change nothing — the assistant, which routes tool calls that only
  SELECT. Every gate in `current_user` decides "is this a write?" from
  the HTTP method, which is right for almost every route and wrong for
  these: without it a viewer asking the assistant a question is told to
  "ask the instance owner to make changes". All three gates (role,
  the gate's read-only standing, forced-2FA enrollment) consult the one
  predicate, so
  they cannot disagree about a route. The bar is that the handler CANNOT
  write, not that it usually does not — a preview or an analyse step that
  stages rows for a later commit stays on the write side.
- `settings_for(role, body)` drops the owner-only fields from a member's
  settings save. The settings door is one endpoint carrying the whole
  document and the clients post all of it on every save, so refusing the
  request would mean a member could never save a budget either.
- `may_restore(role)` gates the ZIP branch of the import doors, which
  depends on the uploaded file rather than the path.

A handful of routes read the role themselves, because their path carries
an id (removing an account) or their meaning depends on the body (the
restore). `server/tests/test_inventory_role_permissions.py` is the check
that keeps this honest: every mutating route must be owner-guarded by
policy, owner-guarded in its handler, or written into `MEMBER_OK`. A new
route that is none of the three fails the test, so it gets triaged by the
person who has the context, while they have it.

Changing somebody's role is step-up authenticated (password, plus a live
code on a TOTP account), the same bar as minting an invite or removing a
member — it is a durable grant, and a stolen cookie must not be enough.
The write itself runs on the admin connection: the app role's UPDATE
grant on `users` is column-scoped precisely so an injected statement
cannot rewrite a privilege column.

## Where it shows in the clients

Both clients ask two different questions, and asking the wrong one is a
real bug in both directions — `isOwner` on an editing control hides it
from the person the member role exists for; `canEdit` on an owner control
shows a member a button that can only 403.

- web: `canEdit` / `isOwner` in `webapp/src/role.ts`
- mobile: `useViewer` / `useOwner` in `mobile/src/lib/viewer.ts`

Both fail closed while `/api/me` is in flight: until the server says who
you are, you are the least-privileged reader.

Settings is the surface where this is most visible. A member gets the
sections that hold the household's money — categorisation, merchants, the
roster, maintenance, their own security — plus their own daily-email
switch. The sections that are the account (connections and their keys,
the mail plumbing, exports and bundles, the AI endpoint, the account
section, the instance's health, the setup wizard that starts by connecting
a bank) are
not rendered for them at all, because every field in them would be
dropped or refused on save, and a control that quietly does nothing is
worse than one that is not there.
