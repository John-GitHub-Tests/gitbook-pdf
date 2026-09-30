#!/usr/bin/env bash
# Fetch the Mermaid bundle used to render diagrams in the PDFs.
#
# The bundle is several MB, so it is fetched on demand rather than committed.
# Without it pdf/build.py still succeeds: a page containing a ```mermaid fence
# shows the diagram source instead of a rendered figure. No page in this space
# uses one today, which is why this is optional rather than a build dependency.
set -euo pipefail

MERMAID_VERSION="${MERMAID_VERSION:-11.16.1}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
vendor="$here/vendor"
target="$vendor/mermaid.min.js"

if [[ -f "$target" && -z "${FORCE:-}" ]]; then
  echo "mermaid already present: $target (set FORCE=1 to refetch)"
  exit 0
fi

command -v npm >/dev/null || { echo "error: npm is required" >&2; exit 1; }

mkdir -p "$vendor"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "fetching mermaid@$MERMAID_VERSION"
( cd "$tmp" && npm pack "mermaid@$MERMAID_VERSION" --silent >/dev/null )
tar -xzf "$tmp"/mermaid-*.tgz -C "$tmp" package/dist/mermaid.min.js
mv "$tmp/package/dist/mermaid.min.js" "$target"

echo "installed $target ($(du -h "$target" | cut -f1))"
