#!/usr/bin/env bash
# Advance the vendored `upstream` baseline branch, then merge it into your branch.
#
#   ./.cozmo/sync-upstream.sh          # track upstream main
#   ./.cozmo/sync-upstream.sh v0.9.6   # track a tag or branch
#   git merge upstream                 # then integrate
set -euo pipefail

UPSTREAM_URL=${UPSTREAM_URL:-https://github.com/xinnan-tech/xiaozhi-esp32-server.git}
UPSTREAM_REF=${1:-main}

git fetch --no-tags "$UPSTREAM_URL" "$UPSTREAM_REF"
sha=$(git rev-parse FETCH_HEAD)
tree=$(git rev-parse "FETCH_HEAD^{tree}")
base=$(git rev-parse refs/heads/upstream)

if [ "$(git rev-parse "$base^{tree}")" = "$tree" ]; then
  echo "upstream already at ${sha:0:10} — nothing to do"
  exit 0
fi

commit=$(git commit-tree "$tree" -p "$base" \
  -m "upstream: xiaozhi-esp32-server $UPSTREAM_REF @ ${sha:0:10}")
git update-ref refs/heads/upstream "$commit" "$base"

echo "upstream: ${base:0:10} -> ${commit:0:10}  (${UPSTREAM_REF} @ ${sha:0:10})"
echo "next: git merge upstream"
