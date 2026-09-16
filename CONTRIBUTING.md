# Contributing

Source-available under FSL-1.1-ALv2; contributions are accepted under the
Developer Certificate of Origin (sign off your commits: `git commit -s`).

**Each commit here is a release** (see [HISTORY.md](HISTORY.md)), so
`git log` is a release timeline — worth knowing before you go looking for
the commit that introduced something. Issues and pull requests are read and
answered normally; an accepted change ships in the next release, credited
in its message.

## Ground rules

- **The test suite is the spec.** Many tests freeze settled behavior
  (sign conventions, the variable-only verdict, occurrence matching,
  dedup rules). Never weaken
  a test to make a change pass — if a test blocks you, open an issue
  first.
- The money conventions live in DEVELOPMENT.md ("Money conventions") — sign
  convention, what spend excludes, how a bill matches its charges — read
  them before touching `engine/`.
- Run before every PR: **`make test`** from the repo root. It needs the dev
  Postgres — setup is in [DEVELOPMENT.md](DEVELOPMENT.md), not the README.
  Prefer it over a bare `python -m unittest discover`: `make test` runs the
  linter first (so a stray import fails in a second rather than after the
  suite) and refuses to start while another suite is running against the
  same test database, which otherwise produces phantom failures.
- `docs/*.md` double as the in-app Help (the `?` in the header) — when a
  feature changes user-visible behavior, update the matching doc in the
  same PR.

## Good first contributions

Importer schemas for banks and apps the importer does not auto-detect yet,
doctor checks, docs fixes, reverse-proxy recipes.
