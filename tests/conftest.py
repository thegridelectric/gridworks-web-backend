"""An ephemeral TimescaleDB (testcontainers) shaped by the gw_data models,
seeded with two houses and three logins, a layouts row per house, and an
app with the v2 routers whose database is that container.

The schema comes from `Base.metadata.create_all`, not the alembic chain;
gridworks-data's own suite proves the models match the migration head.
`cached_hourly_data` is a stub with the columns the hourly-electricity
query reads (on journaldb it is a table refreshed by a timescale job).
Set `WEB_BACKEND_TEST_PG_URL` to use an already-running TimescaleDB
instead; without either, the tests self-skip.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path
import uuid

import bcrypt
from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import create_engine, insert, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "timescale/timescaledb-ha:pg18-ts2.25"

# The testcontainers reaper bind-mounts the Docker socket, which Docker
# Desktop on macOS refuses. The fixture stops its own container.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

# api.v2.dependencies builds Settings at import; the suite never reaches
# a broker or the alert-manager, so stand-in values are enough.
for key, value in {
    "BACKEND_JOURNAL_DB_PASSWORD": "x",
    "BACKEND_BACKOFFICE_DB_PASSWORD": "x",
    "BACKEND_ACCESS_TOKEN_SECRET": "test-secret",
    "BACKEND_TSDB_URL": "postgresql+asyncpg://unused",
    "BACKEND_RABBIT_USER": "x",
    "BACKEND_RABBIT_PASSWORD": "x",
    "BACKEND_RABBIT_HOST": "localhost",
    "BACKEND_RABBIT_PORT": "5671",
    "BACKEND_RABBIT_VHOST": "x",
    "BACKEND_RUNNING_LOCALLY": "True",
    "BACKEND_ALERT_MANAGER_URL": "http://localhost:1",
    "BACKEND_ALERT_MANAGER_TOKEN": "x",
    "BACKEND_MAX_READING_POINTS": "50000",
    "BACKEND_PRICE_FORECAST_URL": "http://localhost:1/hw1-isone-price/latest-forecast",
    "BACKEND_SCADA_REPO": "thegridelectric/gridworks-scada",
}.items():
    os.environ.setdefault(key, value)

import httpx  # noqa: E402

import gw_data.db.models  # noqa: E402,F401  (registers every model on Base)
from gw_data.db.models import GNodeSql, HouseOperatingStatusSql, LayoutSql, MessageSql, UserInstallationRoleSql, UserSql  # noqa: E402
from gw_data.db.models._base import Base  # noqa: E402

from api.v2.dependencies import get_db  # noqa: E402
from api.v2.routers import (  # noqa: E402
    hourly_electricity, installation_summaries, price_forecast, readings_csv, session, synced_readings_bundle,
)

BEECH = "hw1.isone.me.versant.keene.beech"
OAK = "hw1.isone.me.versant.keene.oak"
BEECH_TA_ID = uuid.uuid4()
OAK_TA_ID = uuid.uuid4()
PASSWORD = "pw"
SNAPSHOT_TIME_MS = 1774893120000  # the snapshot.spaceheat.003 sample


class Seed:
    """What the seed put in the database, for the tests to assert against."""

    houses = {BEECH: BEECH_TA_ID, OAK: OAK_TA_ID}
    # username -> (role, house alias or None for every house)
    logins = {
        "sysadmin": ("admin", None),
        "beech-owner": ("owner", BEECH),
        "beech-viewer": ("viewer", BEECH),
    }


BEECH_SCADA_COMMIT = "e55faa2d3b1c4f6a8d9e0f1a2b3c4d5e6f7a8b9c"
OLD_SCADA_COMMIT = "0123456789abcdef0123456789abcdef01234567-dirty"


def scada_commit(house_alias: str, commit: str, at: datetime) -> dict:
    """A `gw.scada.commit` announcement from the house's scada."""
    return {
        "ScadaAlias": house_alias + ".scada",
        "GitCommit": commit,
        "MessageCreatedMs": int(at.timestamp() * 1000),
        "TypeName": "gw.scada.commit",
        "Version": "000",
    }


def sample(name: str) -> dict:
    return json.loads((ROOT / "api" / "sema" / "samples" / name).read_text())


def seed(engine) -> None:
    now = datetime.now(tz=UTC)
    with engine.begin() as conn:
        for alias, ta_id in Seed.houses.items():
            house = alias.split(".")[-1]
            conn.execute(insert(GNodeSql), [
                {"id": uuid.uuid4(), "alias": alias, "base_class": "LeafTransactiveNode",
                 "g_node_class": "LeafTransactiveNode", "status": "Active",
                 "display_name": house.capitalize()},
                {"id": ta_id, "alias": alias + ".ta", "base_class": "TerminalAsset",
                 "g_node_class": "TerminalAsset", "status": "Active",
                 "display_name": house.capitalize()},
            ])
        for username, (role, house_alias) in Seed.logins.items():
            user_id = uuid.uuid4()
            conn.execute(insert(UserSql), {
                "id": user_id, "username": username, "is_active": True,
                "hashed_password": bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt()).decode(),
            })
            conn.execute(insert(UserInstallationRoleSql), {
                "user_id": user_id, "role": role,
                "ta_g_node_id": Seed.houses[house_alias] if house_alias else None,
            })
        # Both houses have a layouts row: beech's is 012, oak's is 013 (sema
        # has no example for 013, so the instance is this folder's own). Only
        # beech has a snapshot. The one layout.lite in messages is oak's and
        # is 012, so a reader that scans messages for the layout disagrees
        # with one that reads the layouts row.
        layout = sample("layout.lite.012.json")
        layout_013 = json.loads((ROOT / "tests" / "layout.lite.013.json").read_text())
        snapshot = sample("snapshot.spaceheat.003.json")
        layouts = {BEECH: layout, OAK: layout_013}
        conn.execute(insert(LayoutSql), [
            {"ta_g_node_id": Seed.houses[alias], "terminal_asset_alias": alias + ".ta",
             "from_alias": alias + ".scada", "type_name": payload["TypeName"],
             "version": payload["Version"], "payload": payload,
             "message_id": None, "sent_at": now - timedelta(hours=2)}
            for alias, payload in layouts.items()
        ])
        # Only beech has an operating status row, sent by its scada.
        status = {**sample("gw.house.operating.status.000.json"), "ScadaAlias": BEECH + ".scada"}
        conn.execute(insert(HouseOperatingStatusSql), {
            "ta_g_node_id": BEECH_TA_ID, "terminal_asset_alias": BEECH + ".ta",
            "scada_alias": BEECH + ".scada", "source": "scada",
            "type_name": status["TypeName"], "version": status["Version"], "payload": status,
            "message_id": None,
            "created_at": datetime.fromtimestamp(status["CreatedMs"] / 1000, tz=UTC),
        })
        # Beech's scada announced its commit twice (a restart on newer code);
        # the newest announcement is the one the summary states. Oak's scada
        # announced none.
        rows = [
            (BEECH, "snapshot.spaceheat", snapshot, now - timedelta(minutes=5)),
            (OAK, "layout.lite", layout, now - timedelta(hours=1)),
            (BEECH, "gw.scada.commit", scada_commit(BEECH, OLD_SCADA_COMMIT, now - timedelta(days=3)), now - timedelta(days=3)),
            (BEECH, "gw.scada.commit", scada_commit(BEECH, BEECH_SCADA_COMMIT, now - timedelta(hours=3)), now - timedelta(hours=3)),
        ]
        conn.execute(insert(MessageSql), [
            {"id": uuid.uuid4(), "timestamp": ts, "from_alias": alias + ".scada",
             "persisted_at": ts, "message_type_name": type_name, "payload": payload}
            for alias, type_name, payload, ts in rows
        ])
        conn.execute(text(
            "CREATE TABLE gridworks.cached_hourly_data ("
            "terminal_asset_alias text, time_bucket timestamptz, "
            "hp_kwh_el float, total_usd_per_mwh float)"
        ))
        bucket = (now - timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        conn.execute(text(
            "INSERT INTO gridworks.cached_hourly_data VALUES "
            "(:beech, :t, 1.5, 40.0), (:oak, :t, 2.5, 50.0)"
        ), {"beech": BEECH + ".ta", "oak": OAK + ".ta", "t": bucket})


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """A psycopg2 (sync) URL to the seeded database."""
    override = os.environ.get("WEB_BACKEND_TEST_PG_URL")
    if override:
        yield override
        return
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:
        pytest.skip("testcontainers not installed")
    try:
        pg = PostgresContainer(IMAGE, driver="psycopg2", username="gw_admin", password="gw_admin", dbname="tsdb")
        pg.start()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"could not start a testcontainers TimescaleDB: {e}")
    try:
        url = pg.get_connection_url()
        eng = create_engine(url, isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.execute(text("CREATE SCHEMA IF NOT EXISTS gridworks"))
            # the bundle's time-weighted averages (time_weight, interpolated_average)
            c.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb_toolkit"))
        Base.metadata.create_all(eng)
        seed(eng)
        eng.dispose()
        yield url
    finally:
        pg.stop()


@pytest.fixture
def app(pg_url: str) -> FastAPI:
    engine = create_async_engine(async_url(pg_url))
    maker = async_sessionmaker(bind=engine, expire_on_commit=False)

    async def test_db():
        async with maker() as s:
            yield s

    app = FastAPI()
    for router in (
        installation_summaries.router, session.router, hourly_electricity.router, readings_csv.router,
        synced_readings_bundle.router, price_forecast.router,
    ):
        app.include_router(router)
    app.dependency_overrides[get_db] = test_db
    return app


@pytest.fixture
async def client(app: FastAPI):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


def async_url(pg_url: str) -> str:
    return pg_url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")


@pytest.fixture
def db_session(pg_url: str):
    """An async session on the seeded database for calling query helpers directly."""
    engine = create_async_engine(async_url(pg_url))
    return async_sessionmaker(bind=engine, expire_on_commit=False)


async def login(client: httpx.AsyncClient, username: str) -> dict[str, str]:
    r = await client.post("/api/v2/sessions", data={"username": username, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


class GitHubStub:
    """Stands in for httpx.AsyncClient in the commit-standing resolver: a
    compare call for a commit answers with the status set for it, 404 for
    a commit it has no answer for, or raises `error`."""

    statuses: dict[str, str] = {}
    error: Exception | None = None
    calls: list[str] = []

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, headers: dict | None = None) -> httpx.Response:
        commit = url.rsplit("...", 1)[1]
        GitHubStub.calls.append(commit)
        if GitHubStub.error is not None:
            raise GitHubStub.error
        status = GitHubStub.statuses.get(commit)
        if status is None:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json={"status": status})


@pytest.fixture(autouse=True)
def github(monkeypatch) -> type[GitHubStub]:
    """Every test asks the stub, never GitHub; beech's commit is main's head
    unless a test says otherwise. The resolver's cache starts empty."""
    from api.v2 import commit_standing
    from api.v2.routers import installation_summaries

    GitHubStub.statuses = {BEECH_SCADA_COMMIT: "identical"}
    GitHubStub.error = None
    GitHubStub.calls = []
    monkeypatch.setattr(commit_standing, "AsyncClient", GitHubStub)
    monkeypatch.setattr(installation_summaries, "commit_standings", None)
    return GitHubStub
