#!/usr/bin/env bash
# Prepare a Teleport checkout with the Beams plugin wired in, so `go build`
# and `go test` work. The checkout lives in .teleport/ (git-ignored).
#
#   scripts/teleport-tree.sh         clone (first run), reset, apply, link
#   scripts/teleport-tree.sh patch   write upstream.patch from the checkout
#
# plugin/*.go are symlinked into integrations/access/slack/, so edits you make
# in either place land in plugin/. Changes to Teleport's own files in that
# directory are captured with `patch`.
set -euo pipefail

here="$(cd "$(dirname "$0")/.." && pwd)"
ref="$(sed -n 's/^ARG TELEPORT_REF=//p' "$here/Dockerfile")"
tree="${TELEPORT_TREE:-$here/.teleport}"
pkg="integrations/access/slack"

if [[ "${1:-}" == "patch" ]]; then
  git -C "$tree" diff -- "$pkg" > "$here/upstream.patch"
  echo "wrote upstream.patch ($(grep -c '^diff --git' "$here/upstream.patch") files)"
  exit 0
fi

if [[ ! -d "$tree/.git" ]]; then
  echo "cloning Teleport $ref into $tree (shallow, takes a minute)"
  git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$ref" \
    https://github.com/gravitational/teleport.git "$tree"
fi

cd "$tree"
git checkout --quiet -- "$pkg"
git clean -fdq -- "$pkg"
git apply "$here/upstream.patch"
ln -sf "$here"/plugin/*.go "$pkg/"
echo "ready: $tree/$pkg (Teleport $ref + upstream.patch + plugin/)"
