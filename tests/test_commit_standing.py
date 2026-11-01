"""The commit-standing resolver turns GitHub's compare answer into
`gw.commit.standing`, never asks about a dirty commit, and asks once per
commit per TTL."""

import httpx
import pytest

from api.sema.enums import CommitStanding
from api.v2 import commit_standing
from api.v2.commit_standing import CommitStandings

HEAD = "a" * 40
OLD = "b" * 40
BRANCH = "c" * 40
FORK = "d" * 40
UNPUSHED = "e" * 40
DIRTY = HEAD + "-dirty"


async def test_compare_statuses_map_to_standings(github):
    github.statuses = {HEAD: "identical", OLD: "behind", BRANCH: "ahead", FORK: "diverged"}
    standings = await CommitStandings("owner/repo").standings({HEAD, OLD, BRANCH, FORK, UNPUSHED, DIRTY})
    assert standings == {
        HEAD: CommitStanding.OnMainHead,
        OLD: CommitStanding.BehindMain,
        BRANCH: CommitStanding.OffMain,
        FORK: CommitStanding.OffMain,
        UNPUSHED: CommitStanding.Unknown,
        DIRTY: CommitStanding.Dirty,
    }
    assert DIRTY not in github.calls


async def test_unreachable_github_is_unknown(github):
    github.error = httpx.ConnectError("refused")
    standings = await CommitStandings("owner/repo").standings({HEAD})
    assert standings == {HEAD: CommitStanding.Unknown}


async def test_a_standing_is_asked_once_until_its_ttl_passes(github, monkeypatch):
    github.statuses = {HEAD: "identical"}
    resolver = CommitStandings("owner/repo")
    await resolver.standings({HEAD})
    await resolver.standings({HEAD})
    assert github.calls == [HEAD]

    clock = [0.0]
    monkeypatch.setattr(commit_standing.time, "monotonic", lambda: clock[0])
    resolver = CommitStandings("owner/repo")
    await resolver.standings({HEAD})
    clock[0] = commit_standing.STANDING_TTL_SECONDS + 1
    github.statuses = {HEAD: "behind"}
    standings = await resolver.standings({HEAD})
    assert standings == {HEAD: CommitStanding.BehindMain}
    assert github.calls == [HEAD, HEAD, HEAD]


async def test_unknown_is_cached_for_the_ttl_too(github):
    """Unknown is a standing too, and is cached like any other for the TTL;
    the test pins that so a GitHub outage does not become a request storm."""
    resolver = CommitStandings("owner/repo")
    await resolver.standings({UNPUSHED})
    await resolver.standings({UNPUSHED})
    assert github.calls == [UNPUSHED]
