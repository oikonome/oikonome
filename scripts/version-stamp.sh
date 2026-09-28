# Shared version-stamp helpers — sourced by install.sh and oikonome.sh so the
# installer and the control script can never drift apart on what version an
# instance is running. Not executable on its own; there is nothing to run.
#
# A version is `vA.B.N`. `git describe` reports the nearest version tag plus
# how far HEAD is past it — "vA.B.C-M-gSHA" — and the stamp people read is
# the two collapsed together: vA.B.(C+M). An exact tag, a bare SHA, or
# anything else unrecognized passes through untouched.
#
# Two details are load-bearing:
#
#   * The patch is BASE + commits-since, not commits-since alone, so
#     numbering continues from the nearest version tag whatever its patch
#     number is.
#
#   * The tag glob matches ANY version series rather than one hard-coded
#     anchor. That is what lets the numbering move from one series to the
#     next without a flag day: until the next series' vA.B.0 tag exists,
#     describe still finds the old series and the arithmetic is unchanged;
#     the moment that tag lands, describe finds it instead and the stamp
#     follows automatically.

# Glob for `git describe --match`. Deliberately loose on the numbers — the
# collapse below is what actually validates the shape.
VERSION_TAG_GLOB='v[0-9]*.[0-9]*.[0-9]*'

# collapse_version <git-describe-output>
#
# Prints the stamp. A "-dirty" marker survives the collapse: a modified
# checkout must not be able to claim it is a clean release.
collapse_version() {
  local raw="${1:-}" dirty="" majmin base since
  case "$raw" in
    *-dirty) dirty="-dirty"; raw="${raw%-dirty}" ;;
  esac
  if printf '%s' "$raw" | grep -qE '^v[0-9]+\.[0-9]+\.[0-9]+-[0-9]+-g[0-9a-f]+$'; then
    majmin=$(printf '%s' "$raw" | sed -E 's/^(v[0-9]+\.[0-9]+)\..*/\1/')
    base=$(printf '%s' "$raw" | sed -E 's/^v[0-9]+\.[0-9]+\.([0-9]+)-.*/\1/')
    since=$(printf '%s' "$raw" | sed -E 's/^v[0-9]+\.[0-9]+\.[0-9]+-([0-9]+)-g.*/\1/')
    printf '%s.%s%s\n' "$majmin" "$((base + since))" "$dirty"
  else
    printf '%s%s\n' "$raw" "$dirty"
  fi
}

# describe_version <repo-dir> [extra git-describe flags…]
#
# The stamp for a checkout, or an empty string when git cannot answer (a
# copy with no .git — callers decide what to fall back to).
describe_version() {
  local dir="${1:-.}"
  shift 2>/dev/null || true
  local raw
  raw=$(git -C "$dir" describe --tags --match "$VERSION_TAG_GLOB" --always \
        "$@" 2>/dev/null) || return 0
  collapse_version "$raw"
}
