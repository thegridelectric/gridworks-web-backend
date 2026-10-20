#!/usr/bin/env bash
#
# Regenerates a vendored sema snapshot from the canonical sema repo via
# `sema snapshot prepare|build`. This repo has two: the REST API's
# (api/sema, seeded by api/sema_seed_request.yaml) and the realtime
# gateway's (gateway/sema, seeded by gateway/sema_seed_request.yaml). A
# vendored tree is generated: edit the seed (or the sema definitions) and
# re-run, never the tree.
#
# The sema CLI refuses to run from a dirty sema checkout and writes only under
# <sema>/output, so a snapshot is reproducible from the sema commit the
# checkout sits on. The build is published-only: a seed that reaches a
# staging word fails here rather than shipping a dev-only snapshot.
#
# Usage:
#   scripts/regen_sema_snapshot.sh                 # both packages, sibling ../sema
#   scripts/regen_sema_snapshot.sh api             # one package
#   SEMA_REPO=/path/to/sema scripts/regen_sema_snapshot.sh gateway
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SEMA_REPO="${SEMA_REPO:-$(cd "${REPO_ROOT}/../sema" 2>/dev/null && pwd || true)}"

if [[ -z "${SEMA_REPO}" || ! -d "${SEMA_REPO}" ]]; then
  echo "error: sema repo not found." >&2
  echo "       set SEMA_REPO=/path/to/sema and re-run (default looked for a" >&2
  echo "       sibling checkout at ${REPO_ROOT}/../sema)." >&2
  exit 1
fi

PACKAGES=("$@")
if [[ ${#PACKAGES[@]} -eq 0 ]]; then
  PACKAGES=(api gateway)
fi

echo "==> sema repo: ${SEMA_REPO} @ $(git -C "${SEMA_REPO}" rev-parse --short HEAD)"

for package in "${PACKAGES[@]}"; do
  case "${package}" in
    api|gateway) ;;
    *) echo "error: unknown package '${package}' (api or gateway)" >&2; exit 1 ;;
  esac
  seed="${REPO_ROOT}/${package}/sema_seed_request.yaml"
  vendor_dir="${REPO_ROOT}/${package}/sema"

  echo "==> package:   ${package}"
  echo "==> seed:      ${seed}"
  (
    cd "${SEMA_REPO}"
    echo "==> sema snapshot prepare"
    uv run sema snapshot prepare "${seed}"
    echo "==> sema snapshot build --package-name ${package}"
    uv run sema snapshot build --package-name "${package}"
  )
  echo "==> mirror ${SEMA_REPO}/output/sema -> ${vendor_dir}"
  rsync -a --delete --exclude='__pycache__' \
    "${SEMA_REPO}/output/sema/" "${vendor_dir}/"
done

echo "==> done. review the diff (git status)."
