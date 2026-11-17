"""The re-run route solves a flo.rerun.request with bruce on this machine and
answers the flo.recommendation word, and is mounted only where the setting
turns it on."""

import importlib.util

from fastapi import FastAPI
import httpx
import pytest

from api.sema.codec import SemaCodec
from api.sema.types import FloRecommendation
from api.v2.routers import flo_rerun

from .conftest import BEECH, FLO_SLOT_S, OAK, flo_params, login, recommendation

bruce_present = importlib.util.find_spec("bruce") is not None
needs_bruce = pytest.mark.skipif(not bruce_present, reason="the rerun dependency group is not installed")


def rerun_request(params: dict, on: list[int] | None = None, off: list[int] | None = None) -> dict:
    """A `flo.rerun.request` around `params`, with the heat pump forced on
    or off at those 0-based steps."""
    return {
        "Params": params,
        "HpForcedOnSteps": list(on or []),
        "HpForcedOffSteps": list(off or []),
        "TypeName": "flo.rerun.request",
        "Version": "000",
    }


def test_the_route_is_off_by_default():
    app = FastAPI()
    flo_rerun.mount(app, "off", running_locally=False)
    assert not any(getattr(r, "path", "").endswith("flo.rerun") for r in app.routes)


def test_an_api_not_running_locally_refuses_to_start_with_the_route_on():
    # The re-run is a developer tool; a deployed API with the setting on
    # fails at start rather than serving it.
    with pytest.raises(RuntimeError, match="running locally"):
        flo_rerun.mount(FastAPI(), "in-process", running_locally=False)


@needs_bruce
def test_the_route_mounts_when_turned_on_locally():
    app = FastAPI()
    flo_rerun.mount(app, "in-process", running_locally=True)
    assert any(getattr(r, "path", "").endswith("flo.rerun") for r in app.routes)


@pytest.fixture
async def rerun_client(app: FastAPI):
    app.include_router(flo_rerun.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


@needs_bruce
async def test_the_journaled_params_solve_to_the_recommendation_word(rerun_client, monkeypatch):
    # bruce caches a transition matrix per tank shape under ~/.config/bruce;
    # a laptop that has not solved this shape builds it once (about 20 s).
    monkeypatch.setenv("CAN_COMPUTE_TRANSITION_MATRICES", "true")
    r = await rerun_client.post(
        f"/api/v2/installations/{BEECH}/flo.rerun",
        json=rerun_request(flo_params(BEECH, FLO_SLOT_S, version="002")),
        headers=await login(rerun_client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    word = SemaCodec().from_dict(r.json(), expect=FloRecommendation)
    assert word.bidder_alias == BEECH
    assert word.optimal_path_list[0].start_unix_s == FLO_SLOT_S
    assert word.store_state_list is not None and word.path_penalties_list is not None
    assert len(word.pq_pairs) == len(word.optimal_path_list) == len(word.store_state_list) == len(word.path_penalties_list)


@needs_bruce
async def test_an_hour_at_a_version_bruce_no_longer_reads_is_422(rerun_client):
    # The journal keeps flo.params 001 hours; bruce's snapshot reads 002 and
    # no 001 word lifts to 002 (it names no forecasts), so the answer says so.
    r = await rerun_client.post(
        f"/api/v2/installations/{BEECH}/flo.rerun",
        json=rerun_request(flo_params(BEECH, FLO_SLOT_S, version="001")),
        headers=await login(rerun_client, "beech-viewer"),
    )
    assert r.status_code == 422
    assert "bruce reads flo.params 002 and this is 001" in r.json()["detail"]


@needs_bruce
async def test_the_forced_steps_are_what_bruce_solves(rerun_client, monkeypatch):
    seen: dict[str, list[int]] = {}

    class Answer:
        def to_dict(self) -> dict:
            return recommendation("001", BEECH, FLO_SLOT_S)

    def fake_recommend(request):
        seen["on"] = list(request.hp_forced_on_steps)
        seen["off"] = list(request.hp_forced_off_steps)
        return Answer()

    monkeypatch.setattr("bruce.service.recommend", fake_recommend)
    r = await rerun_client.post(
        f"/api/v2/installations/{BEECH}/flo.rerun",
        json=rerun_request(flo_params(BEECH, FLO_SLOT_S, version="002"), on=[1], off=[0, 2]),
        headers=await login(rerun_client, "beech-viewer"),
    )
    assert r.status_code == 200, r.text
    assert seen == {"on": [1], "off": [0, 2]}


async def test_another_houses_params_are_refused(rerun_client):
    r = await rerun_client.post(
        f"/api/v2/installations/{OAK}/flo.rerun",
        json=rerun_request(flo_params(BEECH, FLO_SLOT_S)),
        headers=await login(rerun_client, "sysadmin"),
    )
    assert r.status_code == 400
    assert r.json()["detail"] == flo_rerun.NOT_THIS_HOUSE


async def test_a_body_that_is_not_the_word_is_422(rerun_client):
    r = await rerun_client.post(
        f"/api/v2/installations/{BEECH}/flo.rerun",
        json={"TypeName": "flo.params", "Version": "001"},
        headers=await login(rerun_client, "beech-viewer"),
    )
    assert r.status_code == 422
    assert r.json()["detail"].startswith("Not a flo.rerun.request")


async def test_a_viewer_of_another_house_sees_nothing(rerun_client):
    r = await rerun_client.post(
        f"/api/v2/installations/{OAK}/flo.rerun",
        json=rerun_request(flo_params(OAK, FLO_SLOT_S)),
        headers=await login(rerun_client, "beech-viewer"),
    )
    assert r.status_code == 404
