#!/usr/bin/env bash
# Apply the whole series on NUC2 and push to the fork. Run from /opt/vinted_sniper.
set -euo pipefail
PATCH_DIR="${1:-$(dirname "$0")}"
git checkout -- . 2>/dev/null || true
git checkout main
git pull --ff-only origin main
git checkout -B luc3as/all-in-one
git am "$PATCH_DIR"/000*.patch
git remote get-url fork >/dev/null 2>&1 || git remote add fork git@github.com:Luc3as/vinted-sniper.git
git push -u fork luc3as/all-in-one
docker build -t vinted-sniper:impersonate .
echo "Done. Update the stack in Portainer (no re-pull); add VINTED_SNIPER_TIMEZONE=Europe/Bratislava."
