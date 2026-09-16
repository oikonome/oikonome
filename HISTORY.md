# History

Each commit in this repository is a release, tagged with the version the
running instances report. If you are looking for "why is this line the way
it is", the answer is in the code and its comments.

## Versions

A version is `vA.B.N`, where N counts commits past that series' `vA.B.0`
anchor tag. `git describe` produces it, `install.sh` stamps it into the
environment, and it appears in the app footer, the `/api/me` response, and
the Doctor diagnostic bundle. Each commit here carries the tag of the
version its tree builds, so an instance installed from this repository
reports exactly that version.

For what shipped in a version, read its commit and its release notes — or
the Doctor page of a running instance, which reports the exact version it
is serving. The admin console's version-history panel shows the same
release list, read from a manifest baked into the package because the
container image carries no `.git`.
