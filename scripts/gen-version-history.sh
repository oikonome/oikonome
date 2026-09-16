#!/usr/bin/env bash
# Generate the version-history manifest the admin console renders.
#
# The container image carries no .git (docker/Dockerfile copies only
# server/oikonome, webapp/dist and docs), so the console cannot shell out to
# git at runtime. This bakes the history into a file that ships inside the
# package instead.
#
# Every vA.B.N is simply the Nth commit after that series' vA.B.0 tag — the
# same mapping install.sh uses when it collapses `git describe` into a clean
# semver (scripts/version-stamp.sh). So the manifest is just `git log`
# numbered from the tag, plus a running lines-of-code total (tracked text
# files) at each commit. The walk always starts at the FIRST anchor below,
# and switches series wherever a later vA.B.0 tag lands, so versions that
# have already shipped are never renumbered.
#
# The output is GENERATED and gitignored. install.sh rebuilds it on every
# install/upgrade, so the running instance's panel is current.
#
# Without the anchor tag this exits without touching the output, so a
# checkout that lacks the tag keeps whatever manifest it already has
# rather than overwriting it with nothing.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=server/oikonome/version_history.tsv
TAG=v0.1.0            # the oldest anchor — where the numbering starts, not
                      # the series the current code is in

if ! git rev-parse "$TAG" >/dev/null 2>&1; then
  echo "gen-version-history: tag $TAG not found — leaving $OUT untouched" >&2
  exit 0
fi

python3 "$(dirname "$0")/gen_version_history.py" "$TAG" > "$OUT"

echo "gen-version-history: wrote $(($(wc -l < "$OUT") - 3)) versions to $OUT"
