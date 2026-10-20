"""The vendored sema snapshots ship only published vocabulary on `main`.

A snapshot built with `--allow-staged` is for dev brokers only (sema spec,
snapshot "Published-only by default"); web-api2 is production and deploys
`main`. Every word in each snapshot's expanded seed is looked up in the
sema registry rather than trusting the snapshot's own staging marker, so a
hand-built snapshot with no marker is caught too, as is a word the
registry no longer has (a staging word can be withdrawn). The registry comes from
`SEMA_PUBLIC_REGISTRY` or the sibling sema checkout; without either the
test self-skips. Off `main` the staging words are reported, not failed.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOTS = {"api": ROOT / "api" / "sema", "gateway": ROOT / "gateway" / "sema"}
PRODUCTION_BRANCH = "main"


def registry_path() -> Path | None:
    override = os.environ.get("SEMA_PUBLIC_REGISTRY")
    if override:
        return Path(override)
    sibling = ROOT.parent / "sema" / "indexes" / "public_registry.yaml"
    return sibling if sibling.exists() else None


def current_branch() -> str:
    """The branch under test: GitHub Actions' ref (the PR's target on a
    pull request), else the checkout's."""
    for key in ("GITHUB_BASE_REF", "GITHUB_REF_NAME"):
        if os.environ.get(key):
            return os.environ[key]
    out = subprocess.run(
        ["git", "-C", str(ROOT), "branch", "--show-current"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def staging_words(snapshot: Path, registry: dict) -> list[str]:
    """Every word/version in the snapshot's expanded seed whose registry
    status is not published. Mirrors the sema CLI's staging-closure walk."""
    seed = yaml.safe_load((snapshot / "indexes" / "seed_expanded.yaml").read_text())
    worklist = seed.get("worklist", {})
    found: list[str] = []

    def status_of(entry: dict | None, version: str | None) -> str:
        if entry is None:
            return "not in registry"
        if version is None:
            return entry["status"]
        version_entry = entry.get("versions", {}).get(version)
        return "not in registry" if version_entry is None else version_entry["status"]

    for name, spec in (worklist.get("enums") or {}).items():
        entry = registry["enums"].get(name)
        if entry is not None and entry.get("enum_type") == "literal":
            if (status := status_of(entry, None)) != "published":
                found.append(f"enum {name} ({status})")
            continue
        for version in spec:
            if (status := status_of(entry, version)) != "published":
                found.append(f"enum {name}:{version} ({status})")

    for name, spec in (worklist.get("types") or {}).items():
        entry = registry["types"].get(name)
        if "path" in spec:  # versionless type
            if (status := status_of(entry, None)) != "published":
                found.append(f"type {name} ({status})")
            continue
        for version in spec:
            if (status := status_of(entry, version)) != "published":
                found.append(f"type {name}:{version} ({status})")

    return sorted(found)


@pytest.mark.parametrize("package", sorted(SNAPSHOTS))
def test_snapshot_is_published_only_on_main(package: str) -> None:
    path = registry_path()
    if path is None:
        pytest.skip("no sema registry: set SEMA_PUBLIC_REGISTRY or check out ../sema")
    registry = yaml.safe_load(path.read_text())

    offenders = staging_words(SNAPSHOTS[package], registry)
    if not offenders:
        return
    report = f"{package}/sema holds non-published words:\n  " + "\n  ".join(offenders)
    branch = current_branch()
    if branch == PRODUCTION_BRANCH:
        pytest.fail(report)
    pytest.skip(f"on {branch!r}, not {PRODUCTION_BRANCH!r}; {report}")
