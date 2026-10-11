#!/usr/bin/env bash
#
# Regenerates the gateway's vendored sema snapshot (gateway/sema) from the
# canonical sema repo via `sema snapshot prepare|build`. The vendored tree is
# generated: edit gateway/sema_seed_request.yaml (or the sema definitions)
# and re-run, never the tree.
#
# The sema CLI refuses to run from a dirty sema checkout and writes only under
# <sema>/output, so the snapshot is reproducible from the sema commit the
# checkout sits on.
#
# Usage:
#   scripts/regen_sema_snapshot.sh                 # sibling ../sema checkout
#   SEMA_REPO=/path/to/sema scripts/regen_sema_snapshot.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SEMA_REPO="${SEMA_REPO:-$(cd "${REPO_ROOT}/../sema" 2>/dev/null && pwd || true)}"

PACKAGE_NAME="gateway"
SEED="${REPO_ROOT}/gateway/sema_seed_request.yaml"
VENDOR_DIR="${REPO_ROOT}/gateway/sema"

if [[ -z "${SEMA_REPO}" || ! -d "${SEMA_REPO}" ]]; then
  echo "error: sema repo not found." >&2
  echo "       set SEMA_REPO=/path/to/sema and re-run (default looked for a" >&2
  echo "       sibling checkout at ${REPO_ROOT}/../sema)." >&2
  exit 1
fi

echo "==> sema repo: ${SEMA_REPO} @ $(git -C "${SEMA_REPO}" rev-parse --short HEAD)"
echo "==> seed:      ${SEED}"
echo "==> package:   ${PACKAGE_NAME}"

cd "${SEMA_REPO}"
echo "==> sema snapshot prepare"
# layout.lite 013 and its closure are staging words; the flag admits them.
# Drop it when 013 publishes.
uv run sema snapshot prepare --allow-staged "${SEED}"
echo "==> sema snapshot build --package-name ${PACKAGE_NAME}"
uv run sema snapshot build --package-name "${PACKAGE_NAME}"

echo "==> mirror ${SEMA_REPO}/output/sema -> ${VENDOR_DIR}"
rsync -a --delete --exclude='__pycache__' \
  "${SEMA_REPO}/output/sema/" "${VENDOR_DIR}/"

echo "==> done. review the diff (git status)."
