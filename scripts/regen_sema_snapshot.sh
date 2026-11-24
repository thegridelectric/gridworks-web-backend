#!/usr/bin/env bash
#
# regen_sema_snapshot.sh — regenerate the vendored Sema snapshots of the
# api (api/sema) and the gateway (gateway/sema) from the canonical sema
# repo, with `sema snapshot regen` on each package's sema_seed_request.yaml.
# Each seed states its staging policy (none: published-only), the package
# name and the vendor dir; there is no flag to pass.
#
# The vendored trees are GENERATED: never hand-edit them; edit the seed (or
# the sema definitions) and re-run.
#
# The CLI refuses to run from a dirty sema checkout, so a snapshot is
# reproducible from the sema commit the checkout sits on; its README
# records it.
#
# Usage:
#   scripts/regen_sema_snapshot.sh                 # both, sibling ../sema
#   scripts/regen_sema_snapshot.sh api             # one package
#   SEMA_REPO=/path/to/sema scripts/regen_sema_snapshot.sh
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
  echo "==> package:   ${package}"
  (cd "${SEMA_REPO}" && uv run sema snapshot regen "${REPO_ROOT}/${package}/sema_seed_request.yaml")
done
echo "==> done. review the diff (git status) and run ./ci.sh."
