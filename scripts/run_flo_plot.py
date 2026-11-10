#!/usr/bin/env python3
"""Fetch flo.params for a time slot, run the optimizer, and show graph + bid plots.

Run from the repo root (needs .env with BACKEND_TSDB_URL, same as the API):

    uv run python scripts/run_flo_plot.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# --- edit these ---
INSTALLATION_ID = "hw1.isone.me.versant.keene.oak"
RUN_AT = datetime(2026, 11, 6, 20, 0, tzinfo=ZoneInfo("America/New_York"))
# ------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv

load_dotenv(_REPO_ROOT / ".env")

from sqlalchemy import BigInteger, cast, create_engine, desc, or_, select
from sqlalchemy.orm import Session

from api.config import Settings
from bruce.flo_params import FloParamsError, heat_pump_params_from_flo
from bruce.graph_optimizer import Graph, plot_bid
from bruce.graph_optimizer.assets.heat_pump_water_tank import (
    HeatPumpWaterTankAsset,
    plot_graph_results,
)
from gw_data.db.models import MessageSql

_NY = ZoneInfo("America/New_York")


def ny_hour(when: datetime) -> datetime:
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(_NY).replace(minute=0, second=0, microsecond=0)


def sync_journal_url(settings: Settings) -> str:
    raw = settings.tsdb_url.get_secret_value().strip()
    if not raw:
        raise SystemExit("BACKEND_TSDB_URL is not set in .env")
    return raw.replace("postgresql+asyncpg://", "postgresql://", 1)


def fetch_flo_params(session: Session, installation_id: str, hour_start: datetime) -> dict:
    hour_end = hour_start + timedelta(hours=1)
    start_unix_s = cast(MessageSql.payload["StartUnixS"].astext, BigInteger)
    from_aliases = (
        f"{installation_id}.ltn",
        f"{installation_id}.scada",
    )
    row = session.execute(
        select(MessageSql.payload)
        .where(
            MessageSql.message_type_name == "flo.params",
            MessageSql.timestamp >= hour_start - timedelta(days=1),
            MessageSql.timestamp < hour_end + timedelta(days=1),
            start_unix_s >= int(hour_start.timestamp()),
            start_unix_s < int(hour_end.timestamp()),
            or_(
                MessageSql.from_alias.in_(from_aliases),
                MessageSql.payload["GNodeAlias"].astext == installation_id,
            ),
        )
        .order_by(desc(start_unix_s), desc(MessageSql.timestamp))
        .limit(1)
    ).one_or_none()
    if row is None:
        raise SystemExit(
            f"No flo.params for {installation_id} in NY hour "
            f"{hour_start.isoformat()} – {hour_end.isoformat()}"
        )
    payload = row.payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        raise SystemExit("Journal row payload is not a JSON object")
    return payload


def fetch_latest_price_near_hour_start(
    session: Session, hour_start: datetime
) -> tuple[str, datetime, dict] | None:
    """latest.price persisted near the top of the NY hour (any from_alias)."""
    slot_unix = int(hour_start.timestamp())
    slot_name_pattern = f"%.{slot_unix}"
    window_before = timedelta(minutes=5)
    window_after = timedelta(minutes=30)
    row = session.execute(
        select(MessageSql.from_alias, MessageSql.timestamp, MessageSql.payload)
        .where(
            MessageSql.message_type_name == "latest.price",
            or_(
                MessageSql.payload["MarketSlotName"].astext.like(slot_name_pattern),
                MessageSql.timestamp >= hour_start - window_before,
            ),
            MessageSql.timestamp < hour_start + window_after,
        )
        .order_by(MessageSql.timestamp)
        .limit(1)
    ).one_or_none()
    if row is None:
        return None
    payload = row.payload
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return None
    return row.from_alias, row.timestamp, payload


def real_price_mwh_from_latest_price(payload: dict) -> float | None:
    raw = payload.get("PriceTimes1000")
    if raw is None:
        return None
    return float(raw) / 1000


def run_optimizer_and_show_plot(
    payload: dict,
    *,
    real_price_mwh: float | None = None,
) -> None:
    params = heat_pump_params_from_flo(payload).model_copy(update={"short_cycle_penalty_enabled": False})
    asset = HeatPumpWaterTankAsset(params)
    graph = Graph(asset)
    try:
        graph.find_shortest_path()
        forecast_price_mwh = params.elec_price_mwh[0]
        pq_pairs = graph.generate_bid(forecast_price_mwh=forecast_price_mwh)
        # plot_graph_results(graph, show=True)
        step_h = params.timestep_duration_hours[0]
        hp_max_quantity_kwh = params.hp_max_kw_elec * step_h
        plot_bid(
            pq_pairs,
            forecast_price_mwh,
            params.bid_min_price_mwh,
            real_price_mwh=real_price_mwh,
            max_quantity_kwh=hp_max_quantity_kwh * 1.1,
            hp_max_quantity_kwh=hp_max_quantity_kwh,
        )
    finally:
        graph.cleanup()


def main() -> None:
    settings = Settings()
    hour_start = ny_hour(RUN_AT)
    engine = create_engine(sync_journal_url(settings), pool_pre_ping=True)

    with Session(engine) as session:
        payload = fetch_flo_params(session, INSTALLATION_ID, hour_start)
        latest_price = fetch_latest_price_near_hour_start(session, hour_start)

    start_s = payload.get("StartUnixS", "?")
    print(f"Fetched flo.params StartUnixS={start_s} for hour {hour_start.isoformat()}")
    if latest_price is None:
        print(
            f"No latest.price near hour start {hour_start.isoformat()} "
            f"(slot unix {int(hour_start.timestamp())})"
        )
    else:
        from_alias, ts, lp = latest_price
        price = lp.get("PriceTimes1000")
        unit = lp.get("PriceUnit")
        slot = lp.get("MarketSlotName")
        print(
            f"Found latest.price from_alias={from_alias} timestamp={ts.isoformat()} "
            f"PriceTimes1000={price} PriceUnit={unit} MarketSlotName={slot}"
        )
    real_price_mwh: float | None = None
    if latest_price is not None:
        real_price_mwh = real_price_mwh_from_latest_price(latest_price[2])

    print("Running optimizer...")
    try:
        run_optimizer_and_show_plot(payload, real_price_mwh=real_price_mwh)
    except FloParamsError as e:
        raise SystemExit(f"Invalid flo.params: {e}") from e


if __name__ == "__main__":
    main()
