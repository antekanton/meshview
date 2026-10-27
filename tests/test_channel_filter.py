"""Channel filter: only channels that have packets to show, unknown channels as one group."""

import json
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from meshview import (
    database,
    models,
    store,
    web,  # noqa: F401  (wires the Packet class into the API module)
)
from meshview.channels import UNKNOWN_GROUP, channel_filter_options
from meshview.web_api import api

HOUR_US = 3600 * 1_000_000
TEXT = 1
POSITION = 3


def test_options_are_busiest_first_with_unknown_channels_folded_last():
    counts = {"? #49": 2, "Ping": 3, "LongFast": 10, "PKI": 3, "? #7": 1, "": 5}

    assert channel_filter_options(counts) == [
        {"channel": "LongFast", "count": 10},
        {"channel": "Ping", "count": 3},
        {"channel": "PKI", "count": 3},
        {"channel": UNKNOWN_GROUP, "count": 3},
    ]


def test_no_unknown_option_without_unknown_channels():
    assert channel_filter_options({"LongFast": 1}) == [{"channel": "LongFast", "count": 1}]


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


async def _add_packets(session_factory, *packets):
    """Each packet is (id, channel, portnum, age_us)."""
    now_us = int(time.time() * 1_000_000)
    async with session_factory() as session:
        for packet_id, channel, portnum, age_us in packets:
            session.add(
                models.Packet(
                    id=packet_id,
                    channel=channel,
                    portnum=portnum,
                    from_node_id=1,
                    payload=b"",
                    import_time_us=now_us - age_us,
                )
            )
        await session.commit()


@pytest.mark.asyncio
async def test_channels_in_period_follow_port_and_window(db):
    await _add_packets(
        db,
        (1, "LongFast", TEXT, 0),
        (2, "LongFast", POSITION, 0),
        (3, "Ping", POSITION, 0),  # no text messages on Ping
        (4, "? #49", TEXT, 0),
        (5, "? #7", TEXT, HOUR_US),
        (6, "Old", TEXT, 25 * HOUR_US),  # outside the 24h window
    )

    assert await store.get_channels_in_period("hour", 24) == [
        {"channel": "LongFast", "count": 2},
        {"channel": "Ping", "count": 1},
        {"channel": UNKNOWN_GROUP, "count": 2},
    ]
    assert await store.get_channels_in_period("hour", 24, portnum=TEXT) == [
        {"channel": "LongFast", "count": 1},
        {"channel": UNKNOWN_GROUP, "count": 2},
    ]


@pytest.mark.asyncio
async def test_packets_filter_by_channel_and_unknown_group(db):
    await _add_packets(
        db,
        (1, "LongFast", TEXT, 0),
        (2, "? #49", TEXT, 0),
        (3, "? #7", TEXT, 0),
        (4, "Ping", TEXT, 0),
    )

    by_name = await store.get_packets(channel="longfast")
    assert [p.id for p in by_name] == [1]

    unknown = await store.get_packets(channel=UNKNOWN_GROUP)
    assert sorted(p.id for p in unknown) == [2, 3]

    assert await store.get_total_packet_count("hour", 24, channel=UNKNOWN_GROUP) == 2


@pytest.mark.asyncio
async def test_node_channels_are_the_names_nodes_report(db):
    async with db() as session:
        for node_id, channel in [(1, "LongFast"), (2, "LongFast"), (3, "Ping"), (4, None)]:
            session.add(models.Node(id=f"!{node_id:08x}", node_id=node_id, channel=channel))
        await session.commit()

    assert await store.get_node_channels() == [
        {"channel": "LongFast", "count": 2},
        {"channel": "Ping", "count": 1},
    ]


@pytest.mark.asyncio
async def test_api_lists_options_and_filters_packets_by_unknown_group(db):
    await _add_packets(
        db,
        (1, "LongFast", TEXT, 0),
        (2, "? #49", TEXT, 0),
        (3, "LongFast", POSITION, 0),
    )

    response = await api.api_channels(SimpleNamespace(query={"portnum": "1"}))
    assert json.loads(response.text) == {
        "channels": ["LongFast", UNKNOWN_GROUP],
        "options": [
            {"channel": "LongFast", "count": 1},
            {"channel": UNKNOWN_GROUP, "count": 1},
        ],
    }

    response = await api.api_packets(SimpleNamespace(query={"channel": UNKNOWN_GROUP}))
    assert [p["id"] for p in json.loads(response.text)["packets"]] == [2]
