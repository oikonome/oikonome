#!/usr/bin/env bash
# Create an isolated worktree for parallel work on this repo.
#
#   ./scripts/new-worktree.sh <name>      e.g. ./scripts/new-worktree.sh money
#
# Gives you: ../oikonome-wt-<name> on branch wt/<name>, with its own git
# index and HEAD, and its own test database exported in .envrc.
#
# Each worktree has its own index and test database, so parallel lines of
# work neither share staged files nor collide in the control-plane tables
# (which have no RLS — see scripts/test-db-guard.sh).
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="${1:-}"
if [ -z "$NAME" ]; then
  echo "usage: $0 <name>   (e.g. money, admin, docs)" >&2
  exit 2
fi
if ! [[ "$NAME" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
  echo "worktree name must be lowercase alphanumeric/dashes: $NAME" >&2
  exit 2
fi

DIR="../oikonome-wt-$NAME"
BRANCH="wt/$NAME"
# the name lands in CREATE DATABASE unquoted, where a hyphen is a syntax
# error — fold anything that isn't a word character to underscore
DB="oikonome_test_$(printf '%s' "$NAME" | tr -c 'a-zA-Z0-9_' '_')"

if [ -e "$DIR" ]; then
  echo "$DIR already exists — pick another name or remove it with:" >&2
  echo "  git worktree remove $DIR" >&2
  exit 1
fi

git worktree add -b "$BRANCH" "$DIR"

# Per-worktree environment. Sourced manually, or automatically by direnv.
cat > "$DIR/.envrc" <<EOF
# Isolated test database for this worktree — control-plane tables have no
# RLS, so sharing one database across parallel suites causes phantom
# failures (see scripts/test-db-guard.sh).
export OIKONOME_TEST_DB=$DB
EOF

cat <<EOF

Worktree ready.

  cd $DIR
  source .envrc          # or: direnv allow
  make test              # runs against $DB, isolated

Branch $BRANCH. Merge back with a normal PR or:
  git -C "$(pwd)" merge $BRANCH

Remove when done:
  git worktree remove $DIR && git branch -d $BRANCH
EOF
