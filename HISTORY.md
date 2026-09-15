# History

**This repository is published per release.** Development happens in a
private repository; each release is published here as a single reviewed
commit, tagged with the version the running instances report.

So `git log` here is a release timeline, not a development log — one entry
per version that actually reached users, rather than every work-in-progress
commit along the way. If you are looking for "why is this line the way
it is", the answer is in the code and its comments, which is where this
project deliberately puts it.

## Versions

A version is `vA.B.N`, where N is the number of commits since that series'
`vA.B.0` anchor tag in the development repository. `git describe` produces
it, `install.sh` stamps it into the environment, and it appears in the app footer, the
`/api/me` response, and the Doctor diagnostic bundle. Each published commit
here carries the tag of the version its tree builds, so an instance
installed from this repository reports exactly the version it was
published as.

For what shipped in a version, read its commit and its release notes — or
the Doctor page of a running instance, which reports the exact version it
is serving. The admin console's version-history panel shows the same
release list, read from a manifest baked into the package because the
container image carries no `.git`.
