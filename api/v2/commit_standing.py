"""Where a scada's announced commit stands against the head of its
repository's main branch, as the `gw.commit.standing` enum, asked of
GitHub and cached."""

import logging
import time

from httpx import AsyncClient, HTTPError

from api.sema.enums import GwCommitStanding

logger = logging.getLogger(__name__)

# A standing is good for this long. Main's head moves, so even a commit
# that was its head is asked about again.
STANDING_TTL_SECONDS = 300

# GitHub's compare statuses for `main...<commit>`, as the enum says them.
COMPARE_STATUS_STANDING = {
    "identical": GwCommitStanding.OnMainHead,
    "behind": GwCommitStanding.BehindMain,
    "ahead": GwCommitStanding.OffMain,
    "diverged": GwCommitStanding.OffMain,
}

DIRTY_SUFFIX = "-dirty"


class CommitStandings:
    """Resolves commits to their standing against `repo`'s main head, one
    GitHub compare call per distinct commit per TTL."""

    def __init__(self, repo: str) -> None:
        self.repo = repo
        self.cache: dict[str, tuple[GwCommitStanding, float]] = {}

    async def standings(self, commits: set[str]) -> dict[str, GwCommitStanding]:
        """The standing of every commit in `commits`."""
        result: dict[str, GwCommitStanding] = {}
        unresolved: list[str] = []
        now = time.monotonic()
        for commit in commits:
            if commit.endswith(DIRTY_SUFFIX):
                result[commit] = GwCommitStanding.Dirty
                continue
            cached = self.cache.get(commit)
            if cached is not None and cached[1] > now:
                result[commit] = cached[0]
            else:
                unresolved.append(commit)
        if unresolved:
            async with AsyncClient(timeout=10) as client:
                for commit in unresolved:
                    standing = await self.ask_github(client, commit)
                    self.cache[commit] = (standing, now + STANDING_TTL_SECONDS)
                    result[commit] = standing
        return result

    async def ask_github(self, client: AsyncClient, commit: str) -> GwCommitStanding:
        """One compare call; Unknown when GitHub does not answer or does not
        know the commit."""
        url = f"https://api.github.com/repos/{self.repo}/compare/main...{commit}"
        try:
            response = await client.get(url, headers={"Accept": "application/vnd.github+json"})
        except HTTPError as e:
            logger.warning("github unreachable for %s: %s", commit, e)
            return GwCommitStanding.Unknown
        if response.status_code != 200:
            logger.warning("github answered %s for %s", response.status_code, commit)
            return GwCommitStanding.Unknown
        status = response.json().get("status")
        standing = COMPARE_STATUS_STANDING.get(status)
        if standing is None:
            logger.warning("github compare status %r for %s", status, commit)
            return GwCommitStanding.Unknown
        return standing
