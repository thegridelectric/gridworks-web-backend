#!/usr/bin/env bash
#
# The gate for this repo. Run it before committing.
#
# The tests stand up a throwaway TimescaleDB in Docker (testcontainers) and
# self-skip when they cannot (README "Tests"). Arguments go to pytest.
#
# Usage: ./ci.sh [pytest args]
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

step() { printf '\n=== %s ===\n' "$1"; shift; "$@"; }

step "uv sync (locked)" uv sync --group dev --locked
# -P keeps the snapshot folder off sys.path: run from inside it, its
# `types/` package would shadow the standard library's.
for package in api gateway; do
  step "sema snapshot staging check: ${package}" uv run python -P \
    "${package}/sema/check_staging.py" "${package}/sema_seed_request.yaml" \
    --deploy-branch main
done
step "pytest" uv run pytest "$@"

printf '\nAll checks passed.\n'
