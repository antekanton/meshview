"""Packet reach (distinct gateways per packet) and active gateway count."""

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
    yield session_factory
    await engine.dispose()


async def _add_receptions(session_factory, *receptions):
    """Each reception is (packet_id, gateway_node_id, age_us)."""
    now_us = int(time.time() * 1_000_000)
    async with session_factory() as session:
        for rx_time, (packet_id, gateway, age_us) in enumerate(receptions):
            session.add(
                models.PacketSeen(
                    packet_id=packet_id,
                    node_id=gateway,
                    rx_time=rx_time,
                    import_time_us=now_us - age_us,
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_reach_counts_each_gateway_once(db):
    await _add_receptions(
        db,
        (100, 1, 0),
        (100, 1, 0),  # same gateway heard it again, e.g. via a relay
        (100, 2, 0),
        (200, 3, 0),
    )

    assert await store.get_packets_reach([100, 200, 300]) == {100: 2, 200: 1}


@pytest.mark.asyncio
async def test_reach_of_no_packets_is_empty(db):
    assert await store.get_packets_reach([]) == {}


@pytest.mark.asyncio
async def test_active_gateways_ignore_receptions_older_than_24h(db):
    await _add_receptions(
        db,
        (100, 1, 1 * HOUR_US),
        (100, 1, 2 * HOUR_US),
        (100, 2, 23 * HOUR_US),
        (200, 3, 25 * HOUR_US),  # last heard over a day ago
    )

    assert await store.get_active_gateway_count() == 2


@pytest.mark.asyncio
async def test_reach_endpoint_returns_reach_per_requested_id(db):
    await _add_receptions(db, (100, 1, 0), (100, 2, 0), (200, 3, 30 * HOUR_US))

    response = await api.api_packets_reach(SimpleNamespace(query={"ids": "100,200,300"}))

    assert response.status == 200
    assert json.loads(response.text) == {
        "reach": {"100": 2, "200": 1, "300": 0},
        "active_gateways": 2,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("ids", ["", "1,x", ",".join(str(i) for i in range(101))])
async def test_reach_endpoint_rejects_bad_ids(db, ids):
    response = await api.api_packets_reach(SimpleNamespace(query={"ids": ids}))

    assert response.status == 400
