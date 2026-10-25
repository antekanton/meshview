"""A packet's channel is identified by its channel hash (#16, docs/adr/0001)."""

import base64
import string

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from meshtastic.protobuf.mesh_pb2 import MeshPacket
from meshtastic.protobuf.mqtt_pb2 import ServiceEnvelope
from meshtastic.protobuf.portnums_pb2 import PortNum
from meshview import models, mqtt_database, mqtt_store
from meshview.channels import ChannelNames, channel_hash

DEFAULT_KEY = base64.b64decode("1PG7OiApB1nwvP+rz05pAQ==")
PRIVATE_KEY = DEFAULT_KEY[:-1] + bytes([0x3E])  # the "AT4=" short key
OTHER_KEY = bytes(range(16))

PING = channel_hash("Ping", PRIVATE_KEY)  # 13
MEDIUM_FAST = channel_hash("MediumFast", DEFAULT_KEY)  # 31
SNIFFER = 0xF9AD2BD8
GATEWAY = 0x7272F826
SENDER = 50


def test_channel_hash_matches_firmware():
    assert channel_hash("LongFast", DEFAULT_KEY) == 8
    assert MEDIUM_FAST == 31
    assert PING == 13


@pytest.fixture
def names(monkeypatch):
    names = ChannelNames(DEFAULT_KEY, [DEFAULT_KEY, PRIVATE_KEY, OTHER_KEY])
    monkeypatch.setattr(mqtt_store, "_channel_names", names)
    return names


@pytest_asyncio.fixture
async def db(monkeypatch, names):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(models.Base.metadata.create_all)
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(mqtt_database, "async_session", session_factory, raising=False)
    monkeypatch.setattr(mqtt_store, "MQTT_GATEWAY_CACHE", set())
    yield session_factory
    await engine.dispose()


def _envelope(packet_id, hash_, reported_name, gateway, reply_id=0, pki=False):
    packet = MeshPacket(id=packet_id, channel=hash_, to=0xFFFFFFFF, pki_encrypted=pki)
    setattr(packet, "from", SENDER)
    packet.rx_time = gateway & 0xFFFF
    packet.decoded.portnum = PortNum.TEXT_MESSAGE_APP
    packet.decoded.payload = b"hi"
    packet.decoded.reply_id = reply_id
    return ServiceEnvelope(packet=packet, channel_id=reported_name, gateway_id=f"!{gateway:08x}")


async def _receive(*envelopes):
    for env in envelopes:
        await mqtt_store.process_envelope(f"msh/2/e/{env.channel_id}/{env.gateway_id}", env)


async def _channel(db, packet_id):
    async with db() as session:
        return (
            await session.execute(
                select(models.Packet.channel).where(models.Packet.id == packet_id)
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_sniffer_reporting_first_does_not_relabel_the_packet(db):
    await _receive(
        _envelope(1, PING, "MediumFast", SNIFFER),
        _envelope(1, PING, "Ping", GATEWAY),
    )
    assert await _channel(db, 1) == "Ping"


@pytest.mark.asyncio
async def test_packet_heard_only_by_sniffers_is_unknown_until_its_name_is_verified(db):
    await _receive(_envelope(1, PING, "MediumFast", SNIFFER))
    assert await _channel(db, 1) == f"? #{PING}"

    # Another packet on the same channel, reported correctly, names the channel...
    await _receive(_envelope(2, PING, "Ping", GATEWAY))
    assert await _channel(db, 2) == "Ping"
    # ...and the earlier sniffer-only packet with it.
    assert await _channel(db, 1) == "Ping"

    # From now on a sniffer-only packet on that channel gets the verified name directly.
    await _receive(_envelope(3, PING, "MediumFast", SNIFFER))
    assert await _channel(db, 3) == "Ping"


@pytest.mark.asyncio
async def test_preset_name_counts_only_with_the_default_key(db, names):
    short_turbo_private = channel_hash("ShortTurbo", PRIVATE_KEY)
    assert not names.verifies("ShortTurbo", short_turbo_private)
    await _receive(_envelope(1, short_turbo_private, "ShortTurbo", SNIFFER))
    assert await _channel(db, 1) == f"? #{short_turbo_private}"

    await _receive(_envelope(2, MEDIUM_FAST, "MediumFast", SNIFFER))
    assert await _channel(db, 2) == "MediumFast"


@pytest.mark.asyncio
async def test_same_name_with_different_keys_is_two_channels(db):
    dacha_private = channel_hash("Dacha", PRIVATE_KEY)
    dacha_other = channel_hash("Dacha", OTHER_KEY)
    assert dacha_private != dacha_other

    await _receive(_envelope(1, dacha_private, "Dacha", GATEWAY))
    assert await _channel(db, 1) == "Dacha"

    await _receive(_envelope(2, dacha_other, "Dacha", GATEWAY))
    assert await _channel(db, 2) == f"Dacha #{dacha_other}"
    assert await _channel(db, 1) == f"Dacha #{dacha_private}"


@pytest.mark.asyncio
async def test_reply_on_an_ambiguous_hash_takes_the_channel_of_the_original(db, names):
    # Two verified names share one hash: only a reception's own name can tell them apart.
    hash_ = channel_hash("Alpha", PRIVATE_KEY)
    beta = next(
        name
        for name in (f"Beta{a}{b}" for a in string.printable for b in string.printable)
        if channel_hash(name, OTHER_KEY) == hash_
    )
    await _receive(
        _envelope(1, hash_, "Alpha", GATEWAY),
        _envelope(2, hash_, beta, GATEWAY + 1),
    )
    assert await _channel(db, 1) == "Alpha"
    assert await _channel(db, 2) == beta

    # Heard only by a sniffer: ambiguous, unless it is a reply.
    await _receive(_envelope(3, hash_, "MediumFast", SNIFFER))
    assert await _channel(db, 3) == f"? #{hash_}"
    await _receive(_envelope(4, hash_, "MediumFast", SNIFFER, reply_id=1))
    assert await _channel(db, 4) == "Alpha"


@pytest.mark.asyncio
async def test_reply_and_original_share_the_channel(db):
    await _receive(
        _envelope(1, PING, "MediumFast", SNIFFER),
        _envelope(1, PING, "Ping", GATEWAY),
        _envelope(2, PING, "Ping", GATEWAY, reply_id=1),
        _envelope(2, PING, "MediumFast", SNIFFER, reply_id=1),
    )
    assert await _channel(db, 1) == await _channel(db, 2) == "Ping"


@pytest.mark.asyncio
async def test_pki_direct_message_is_labelled_pki(db):
    await _receive(
        _envelope(1, 0, "MediumFast", SNIFFER),
        _envelope(1, 0, "PKI", GATEWAY),
    )
    assert await _channel(db, 1) == "PKI"
    await _receive(_envelope(2, 0, "MediumFast", SNIFFER, pki=True))
    assert await _channel(db, 2) == "PKI"


@pytest.mark.asyncio
async def test_verified_names_survive_a_restart(db, names, monkeypatch):
    await _receive(_envelope(1, PING, "Ping", GATEWAY))

    fresh = ChannelNames(DEFAULT_KEY, [DEFAULT_KEY, PRIVATE_KEY, OTHER_KEY])
    monkeypatch.setattr(mqtt_store, "_channel_names", fresh)
    await mqtt_store.load_channel_names()
    await _receive(_envelope(2, PING, "MediumFast", SNIFFER))
    assert await _channel(db, 2) == "Ping"


@pytest.mark.asyncio
async def test_history_is_recomputed_from_payload_and_receptions(db):
    def payload(packet_id, hash_, reply_id=0):
        packet = MeshPacket(id=packet_id, channel=hash_)
        packet.decoded.portnum = PortNum.TEXT_MESSAGE_APP
        packet.decoded.reply_id = reply_id
        return packet.SerializeToString()

    async with db() as session:
        for packet_id, hash_, first_name, reply_id in [
            (1, PING, "MediumFast", 0),  # a sniffer reported first
            (2, PING, "MediumFast", 0),  # only a sniffer heard it
            (3, 49, "MediumFast", 0),  # never verified
            (4, MEDIUM_FAST, "MediumFast", 1),
        ]:
            session.add(
                models.Packet(
                    id=packet_id,
                    from_node_id=SENDER,
                    payload=payload(packet_id, hash_, reply_id),
                    channel=first_name,
                )
            )
        session.add(models.Packet(id=-5, from_node_id=SENDER, payload=b"", channel="MediumFast"))
        for rx_time, (packet_id, gateway, name) in enumerate(
            [(1, SNIFFER, "MediumFast"), (1, GATEWAY, "Ping"), (2, SNIFFER, "MediumFast"),
             (3, SNIFFER, "MediumFast"), (4, SNIFFER, "MediumFast")]
        ):  # fmt: skip
            session.add(
                models.PacketSeen(
                    packet_id=packet_id, node_id=gateway, channel=name, rx_time=rx_time
                )
            )
        await session.commit()

    assert await mqtt_store.recompute_packet_channels(batch_size=2) == 4
    assert [await _channel(db, i) for i in (1, 2, 3, 4, -5)] == [
        "Ping",
        "Ping",
        "? #49",
        "MediumFast",
        "MediumFast",
    ]
    # Second run has nothing left to do.
    assert await mqtt_store.recompute_packet_channels() == 0
