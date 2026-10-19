"""Packet reach and active gateways per channel."""

import datetime
import json
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from meshview import database, models, store
from meshview.web_api import api

HOUR_US = 3600 * 1_000_000
SENDER = 50


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session", session_factory)
    monkeypatch.setattr(api, "_active_gateways_cache", {"expires": 0.0, "by_channel": {}})
    yield session_factory
    await engine.dispose()


async def _add_packets(session_factory, *packets):
    """Each packet is (packet_id, from_node_id, channel)."""
    async with session_factory() as session:
        for packet_id, from_node_id, channel in packets:
            session.add(models.Packet(id=packet_id, from_node_id=from_node_id, channel=channel))
        await session.commit()


async def _add_receptions(session_factory, *receptions):
    """Each reception is (packet_id, gateway_node_id, channel, age_us).

    The channel is stored on the reception but active gateways follow the packet's channel.
    """
    now_us = int(time.time() * 1_000_000)
    async with session_factory() as session:
        for rx_time, (packet_id, gateway, channel, age_us) in enumerate(receptions):
            session.add(
                models.PacketSeen(
                    packet_id=packet_id,
                    node_id=gateway,
                    channel=channel,
                    rx_time=rx_time,
                    import_time_us=now_us - age_us,
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_reach_counts_each_gateway_once_and_skips_the_sender(db):
    await _add_packets(db, (100, SENDER, "LongFast"), (200, SENDER, "LongFast"))
    await _add_receptions(
        db,
        (100, 1, "LongFast", 0),
        (100, 1, "LongFast", 0),  # same gateway heard it again, e.g. via a relay
        (100, 2, "LongFast", 0),
        (100, SENDER, "LongFast", 0),  # the sender uplinking its own packet
        (200, 3, "LongFast", 0),
    )

    assert await store.get_packets_reach([100, 200, 300]) == {
        100: ("LongFast", 2),
        200: ("LongFast", 1),
    }


@pytest.mark.asyncio
async def test_reach_of_unheard_packet_is_zero(db):
    await _add_packets(db, (100, SENDER, "LongFast"))

    assert await store.get_packets_reach([100]) == {100: ("LongFast", 0)}
    assert await store.get_packets_reach([]) == {}


@pytest.mark.asyncio
async def test_active_gateways_are_counted_per_channel_within_the_window(db):
    await _add_packets(db, (100, SENDER, "LongFast"), (200, SENDER, "MediumFast"))
    await _add_receptions(
        db,
        (100, 1, "LongFast", HOUR_US // 2),
        (100, 2, "LongFast", HOUR_US),
        (100, 3, "LongFast", 3 * HOUR_US),  # last heard before a 2h window
        (200, 4, "MediumFast", HOUR_US),
    )

    window = datetime.timedelta(hours=2)
    assert await store.get_active_gateways_by_channel(window) == {"LongFast": 2, "MediumFast": 1}
    window = datetime.timedelta(hours=4)
    assert await store.get_active_gateways_by_channel(window) == {"LongFast": 3, "MediumFast": 1}


@pytest.mark.asyncio
async def test_gateway_uplinking_only_its_own_packets_is_not_active(db):
    await _add_packets(db, (100, SENDER, "LongFast"), (200, 7, "LongFast"))
    await _add_receptions(
        db,
        (100, 1, "LongFast", 0),
        (200, 7, "LongFast", 0),  # node 7 only ever uplinks what it sent itself
    )

    window = datetime.timedelta(hours=2)
    assert await store.get_active_gateways_by_channel(window) == {"LongFast": 1}


@pytest.mark.parametrize(
    ("configured", "hours"),
    [(None, 2), ("6", 6), ("0.5", 0.5), ("0", 2), ("-1", 2), ("soon", 2)],
)
def test_active_gateway_window_comes_from_site_config(monkeypatch, configured, hours):
    site = {} if configured is None else {"active_gateway_hours": configured}
    monkeypatch.setitem(api.CONFIG, "site", site)

    assert api._active_gateway_window() == datetime.timedelta(hours=hours)


@pytest.mark.asyncio
async def test_reach_endpoint_returns_reach_and_channel_active_gateways(db):
    await _add_packets(
        db, (100, SENDER, "LongFast"), (200, SENDER, "LongFast"), (300, SENDER, "MediumFast")
    )
    await _add_receptions(
        db,
        (100, 1, "LongFast", 0),
        (100, 2, "LongFast", 0),
        (200, 3, "LongFast", 0),
        (300, 4, "MediumFast", 0),
    )

    response = await api.api_packets_reach(SimpleNamespace(query={"ids": "100,200,300,999"}))

    assert response.status == 200
    assert json.loads(response.text) == {
        "packets": {
            "100": {"reach": 2, "active_gateways": 3},
            "200": {"reach": 1, "active_gateways": 3},
            "300": {"reach": 1, "active_gateways": 1},
        }
    }


@pytest.mark.asyncio
async def test_active_gateways_are_cached_between_polls(db, monkeypatch):
    calls = 0

    async def counting(window):
        nonlocal calls
        calls += 1
        return {"LongFast": 5}

    monkeypatch.setattr(store, "get_active_gateways_by_channel", counting)

    assert await api._active_gateways_by_channel() == {"LongFast": 5}
    assert await api._active_gateways_by_channel() == {"LongFast": 5}
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", ["", "1,x", ",".join(str(i) for i in range(101))])
async def test_reach_endpoint_rejects_bad_ids(db, ids):
    response = await api.api_packets_reach(SimpleNamespace(query={"ids": ids}))

    assert response.status == 400
