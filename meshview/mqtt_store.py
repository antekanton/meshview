import logging
import re
import time
from datetime import UTC, datetime

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError

from meshtastic.protobuf.config_pb2 import Config
from meshtastic.protobuf.mesh_pb2 import HardwareModel, MeshPacket
from meshtastic.protobuf.portnums_pb2 import PortNum
from meshview import decode_payload, mqtt_database
from meshview.channels import ChannelNames, unknown_label
from meshview.models import ChannelName, DailySnapshot, Node, Packet, PacketSeen, Traceroute

logger = logging.getLogger(__name__)

MQTT_GATEWAY_CACHE: set[int] = set()
UNKNOWN_NODE_NAME = "unknown name"
NODE_NAME_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
MQTT_DIRECT_NODE_ID = 1


_channel_names: ChannelNames | None = None


def channel_names() -> ChannelNames:
    """Verified channel names, keyed by the keys meshview decrypts with."""
    global _channel_names
    if _channel_names is None:
        from meshview.mqtt_reader import PRIMARY_KEY, SECONDARY_KEYS

        _channel_names = ChannelNames(PRIMARY_KEY, [PRIMARY_KEY, *SECONDARY_KEYS])
    return _channel_names


def _insert_ignore(session, model, values, index_elements):
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        return (
            sqlite_insert(model)
            .values(**values)
            .on_conflict_do_nothing(index_elements=index_elements)
        )
    if dialect == "postgresql":
        return (
            pg_insert(model).values(**values).on_conflict_do_nothing(index_elements=index_elements)
        )
    raise NotImplementedError(f"Unsupported dialect: {dialect}")


async def load_channel_names():
    names = channel_names()
    async with mqtt_database.async_session() as session:
        result = await session.execute(select(ChannelName.channel_hash, ChannelName.name))
        for hash_, name in result.all():
            names.learn(hash_, name)


async def _learn_channel_name(session, hash_: int, name: str, relabel: bool = True) -> None:
    """Remember a verified name and relabel packets whose label it changes."""
    names = channel_names()
    hash_was_unnamed = not names.names_by_hash.get(hash_)
    other_hashes = set(names.hashes_by_name.get(name, set()))
    if not names.learn(hash_, name):
        return
    await session.execute(
        _insert_ignore(
            session,
            ChannelName,
            {
                "channel_hash": hash_,
                "name": name,
                "first_seen_us": int(time.time() * 1_000_000),
            },
            ["channel_hash", "name"],
        )
    )
    if not relabel:
        return
    # The name now belongs to two hashes: the old channel's packets get "<name> #<hash>".
    if len(other_hashes) == 1:
        (old_hash,) = other_hashes
        await session.execute(
            update(Packet)
            .where(Packet.channel == name, Packet.channel_hash == old_hash)
            .values(channel=names.label(name, old_hash))
        )
    # The channel was unknown: its packets get the name.
    if hash_was_unnamed:
        await session.execute(
            update(Packet)
            .where(Packet.channel == unknown_label(hash_), Packet.channel_hash == hash_)
            .values(channel=names.label(name, hash_))
        )


async def _resolve_packet_channel(session, packet_id: int, env) -> None:
    """Recompute a packet's channel from all its receptions so far."""
    names = channel_names()
    stored_hash = (
        await session.execute(select(Packet.channel_hash).where(Packet.id == packet_id))
    ).scalar_one_or_none()
    hash_ = stored_hash if stored_hash is not None else env.packet.channel

    reported = set(
        (
            await session.execute(
                select(PacketSeen.channel).where(PacketSeen.packet_id == packet_id).distinct()
            )
        )
        .scalars()
        .all()
    )
    reported.add(env.channel_id)
    for name in reported:
        if names.verifies(name, hash_):
            await _learn_channel_name(session, hash_, name)

    original = None
    reply_id = env.packet.decoded.reply_id
    if reply_id:
        row = (
            await session.execute(
                select(Packet.channel_hash, Packet.channel).where(Packet.id == reply_id)
            )
        ).first()
        if row is not None:
            original = (row[0], row[1])

    label = names.resolve(hash_, reported, pki=env.packet.pki_encrypted, original=original)
    await session.execute(
        update(Packet)
        .where(
            Packet.id == packet_id,
            or_(Packet.channel.is_distinct_from(label), Packet.channel_hash.is_(None)),
        )
        .values(channel=label, channel_hash=hash_)
    )


def normalize_node_name(name: str) -> str:
    name = name.strip()
    if not name or NODE_NAME_CONTROL_CHARS.search(name):
        return UNKNOWN_NODE_NAME
    return name


def storage_packet_id(packet) -> int | None:
    if packet.id:
        return packet.id

    if packet.decoded.portnum != PortNum.MAP_REPORT_APP:
        return None

    from_node_id = getattr(packet, "from", 0) or 0
    now_us = int(time.time() * 1_000_000)
    return -(now_us * 2048 + (from_node_id & 0x7FF))


def storage_to_node_id(packet) -> int:
    if packet.decoded.portnum == PortNum.MAP_REPORT_APP:
        return MQTT_DIRECT_NODE_ID
    return packet.to


async def capture_daily_snapshot() -> None:
    today = datetime.now(UTC).date()

    async with mqtt_database.async_session() as session:
        node_count = (await session.execute(select(func.count()).select_from(Node))).scalar_one()
        packet_count = (
            await session.execute(select(func.count()).select_from(Packet))
        ).scalar_one()
        gateway_count = (
            await session.execute(
                select(func.count()).select_from(Node).where(Node.is_mqtt_gateway.is_(True))
            )
        ).scalar_one()
        captured_at_us = int(time.time() * 1_000_000)
        values = {
            "snapshot_date": today,
            "node_count": node_count,
            "packet_count": packet_count,
            "gateway_count": gateway_count,
            "captured_at_us": captured_at_us,
        }

        dialect = session.get_bind().dialect.name
        stmt = None
        if dialect == "sqlite":
            stmt = (
                sqlite_insert(DailySnapshot)
                .values(**values)
                .on_conflict_do_update(index_elements=["snapshot_date"], set_=values)
            )
        elif dialect == "postgresql":
            stmt = (
                pg_insert(DailySnapshot)
                .values(**values)
                .on_conflict_do_update(index_elements=["snapshot_date"], set_=values)
            )

        if stmt is not None:
            await session.execute(stmt)
        else:
            snapshot = (
                await session.execute(
                    select(DailySnapshot).where(DailySnapshot.snapshot_date == today)
                )
            ).scalar_one_or_none()
            if snapshot is None:
                session.add(DailySnapshot(**values))
            else:
                snapshot.node_count = node_count
                snapshot.packet_count = packet_count
                snapshot.gateway_count = gateway_count
                snapshot.captured_at_us = captured_at_us

        await session.commit()


async def process_envelope(topic, env):
    # MAP_REPORT_APP
    if env.packet.decoded.portnum == PortNum.MAP_REPORT_APP:
        node_id = getattr(env.packet, "from")
        user_id = f"!{node_id:0{8}x}"

        map_report = decode_payload.decode_payload(
            PortNum.MAP_REPORT_APP, env.packet.decoded.payload
        )

        async with mqtt_database.async_session() as session:
            try:
                hw_model = (
                    HardwareModel.Name(map_report.hw_model)
                    if hasattr(HardwareModel, "Name")
                    else "unknown"
                )
                role = (
                    Config.DeviceConfig.Role.Name(map_report.role)
                    if hasattr(Config.DeviceConfig.Role, "Name")
                    else "unknown"
                )
                node = (
                    await session.execute(select(Node).where(Node.node_id == node_id))
                ).scalar_one_or_none()

                now_us = int(time.time() * 1_000_000)

                if node:
                    node.node_id = node_id
                    node.long_name = map_report.long_name
                    node.short_name = map_report.short_name
                    node.hw_model = hw_model
                    node.role = role
                    if not node.channel:
                        node.channel = env.channel_id
                    node.last_lat = map_report.latitude_i
                    node.last_long = map_report.longitude_i
                    node.firmware = map_report.firmware_version
                    node.last_seen_us = now_us
                    if node.first_seen_us is None:
                        node.first_seen_us = now_us
                else:
                    node = Node(
                        id=user_id,
                        node_id=node_id,
                        long_name=map_report.long_name,
                        short_name=map_report.short_name,
                        hw_model=hw_model,
                        role=role,
                        channel=env.channel_id,
                        firmware=map_report.firmware_version,
                        last_lat=map_report.latitude_i,
                        last_long=map_report.longitude_i,
                        first_seen_us=now_us,
                        last_seen_us=now_us,
                    )
                    session.add(node)
            except Exception as e:
                print(f"Error processing MAP_REPORT_APP: {e}")

            await session.commit()

    packet_id = storage_packet_id(env.packet)
    if packet_id is None:
        return

    async with mqtt_database.async_session() as session:
        # --- Packet insert with ON CONFLICT DO NOTHING
        result = await session.execute(select(Packet).where(Packet.id == packet_id))
        packet = result.scalar_one_or_none()
        if not packet:
            now_us = int(time.time() * 1_000_000)
            packet_values = {
                "id": packet_id,
                "portnum": env.packet.decoded.portnum,
                "from_node_id": getattr(env.packet, "from"),
                "to_node_id": storage_to_node_id(env.packet),
                "payload": env.packet.SerializeToString(),
                "import_time_us": now_us,
                "channel": env.channel_id,
            }
            dialect = session.get_bind().dialect.name
            stmt = None

            if dialect == "sqlite":
                stmt = (
                    sqlite_insert(Packet)
                    .values(**packet_values)
                    .on_conflict_do_nothing(index_elements=["id"])
                )
            elif dialect == "postgresql":
                stmt = (
                    pg_insert(Packet)
                    .values(**packet_values)
                    .on_conflict_do_nothing(index_elements=["id"])
                )

            if stmt is not None:
                await session.execute(stmt)
            else:
                try:
                    async with session.begin_nested():
                        session.add(Packet(**packet_values))
                        await session.flush()
                except IntegrityError:
                    pass

        # --- PacketSeen insert with conflict-safe handling

        if not env.gateway_id:
            print("WARNING: Missing gateway_id, skipping PacketSeen entry")
            # Most likely a misconfiguration of a mqtt publisher?
            return
        else:
            node_id = int(env.gateway_id[1:], 16)

        if node_id not in MQTT_GATEWAY_CACHE:
            MQTT_GATEWAY_CACHE.add(node_id)
            await session.execute(
                update(Node).where(Node.node_id == node_id).values(is_mqtt_gateway=True)
            )

        now_us = int(time.time() * 1_000_000)
        seen_values = {
            "packet_id": packet_id,
            "node_id": int(env.gateway_id[1:], 16),
            "channel": env.channel_id,
            "rx_time": env.packet.rx_time,
            "rx_snr": env.packet.rx_snr,
            "rx_rssi": env.packet.rx_rssi,
            "hop_limit": env.packet.hop_limit,
            "hop_start": env.packet.hop_start,
            "topic": topic,
            "import_time_us": now_us,
        }
        dialect = session.get_bind().dialect.name
        seen_stmt = None
        if dialect == "sqlite":
            seen_stmt = (
                sqlite_insert(PacketSeen)
                .values(**seen_values)
                .on_conflict_do_nothing(index_elements=["packet_id", "node_id", "rx_time"])
            )
        elif dialect == "postgresql":
            seen_stmt = (
                pg_insert(PacketSeen)
                .values(**seen_values)
                .on_conflict_do_nothing(index_elements=["packet_id", "node_id", "rx_time"])
            )

        if seen_stmt is not None:
            await session.execute(seen_stmt)
        else:
            try:
                async with session.begin_nested():
                    session.add(PacketSeen(**seen_values))
                    await session.flush()
            except IntegrityError:
                pass

        # MAP_REPORT packets come straight from a node over MQTT and carry no channel hash.
        if packet_id > 0:
            await _resolve_packet_channel(session, packet_id, env)

        # --- NODEINFO_APP handling
        if env.packet.decoded.portnum == PortNum.NODEINFO_APP:
            try:
                user = decode_payload.decode_payload(
                    PortNum.NODEINFO_APP, env.packet.decoded.payload
                )
                if user and user.id:
                    if user.id[0] == "!" and re.fullmatch(r"[0-9a-fA-F]+", user.id[1:]):
                        node_id = int(user.id[1:], 16)
                    else:
                        logger.warning(
                            "Skipping NODEINFO_APP packet %s: unable to determine node_id",
                            env.packet.id,
                        )
                        await session.commit()
                        return

                    hw_model = (
                        HardwareModel.Name(user.hw_model)
                        if user.hw_model in HardwareModel.values()
                        else f"unknown({user.hw_model})"
                    )
                    role = (
                        Config.DeviceConfig.Role.Name(user.role)
                        if hasattr(Config.DeviceConfig.Role, "Name")
                        else "unknown"
                    )

                    node = (
                        await session.execute(select(Node).where(Node.id == user.id))
                    ).scalar_one_or_none()

                    now_us = int(time.time() * 1_000_000)
                    long_name = normalize_node_name(user.long_name)
                    short_name = normalize_node_name(user.short_name)

                    if node:
                        node.node_id = node_id
                        node.long_name = long_name
                        node.short_name = short_name
                        node.hw_model = hw_model
                        node.role = role
                        node.channel = env.channel_id
                        node.last_seen_us = now_us
                        if node.first_seen_us is None:
                            node.first_seen_us = now_us
                    else:
                        node = Node(
                            id=user.id,
                            node_id=node_id,
                            long_name=long_name,
                            short_name=short_name,
                            hw_model=hw_model,
                            role=role,
                            channel=env.channel_id,
                            first_seen_us=now_us,
                            last_seen_us=now_us,
                        )
                        session.add(node)

            except Exception as e:
                print(f"Error processing NODEINFO_APP: {e}")

        # --- POSITION_APP handling
        if env.packet.decoded.portnum == PortNum.POSITION_APP:
            position = decode_payload.decode_payload(
                PortNum.POSITION_APP, env.packet.decoded.payload
            )
            if position and position.latitude_i and position.longitude_i:
                from_node_id = getattr(env.packet, "from")
                node = (
                    await session.execute(select(Node).where(Node.node_id == from_node_id))
                ).scalar_one_or_none()
                if node:
                    now_us = int(time.time() * 1_000_000)
                    node.last_lat = position.latitude_i
                    node.last_long = position.longitude_i
                    node.last_seen_us = now_us
                    if node.first_seen_us is None:
                        node.first_seen_us = now_us
                    session.add(node)

        # --- TRACEROUTE_APP (no conflict handling, normal insert)
        if env.packet.decoded.portnum == PortNum.TRACEROUTE_APP:
            packet_id = env.packet.id
            if packet_id is not None:
                now_us = int(time.time() * 1_000_000)
                session.add(
                    Traceroute(
                        packet_id=packet_id,
                        route=env.packet.decoded.payload,
                        done=not env.packet.decoded.want_response,
                        gateway_node_id=int(env.gateway_id[1:], 16),
                        import_time_us=now_us,
                    )
                )

        await session.commit()


async def recompute_packet_channels(batch_size: int = 2000) -> int:
    """Resolve the channel of stored packets that predate channel hashes (#16).

    Runs once after the migration that added packet.channel_hash: it handles every packet
    whose channel_hash is still empty. First pass reads each packet's hash and learns the
    verified names of all its receptions; second pass labels every packet with the full
    dictionary, so a packet only sniffers heard still gets a name verified elsewhere.
    Returns the number of packets relabelled.
    """
    names = channel_names()
    info: dict[int, tuple[int, bool, int]] = {}  # id -> (hash, pki, reply_id)
    reported: dict[int, set[str]] = {}

    last_id = 0
    while True:
        async with mqtt_database.async_session() as session:
            rows = (
                await session.execute(
                    select(Packet.id, Packet.payload)
                    .where(
                        Packet.channel_hash.is_(None),
                        Packet.payload.is_not(None),
                        Packet.id > last_id,
                    )
                    .order_by(Packet.id)
                    .limit(batch_size)
                )
            ).all()
            if not rows:
                break
            last_id = rows[-1][0]
            ids = [packet_id for packet_id, _ in rows]
            for packet_id, payload in rows:
                packet = MeshPacket()
                try:
                    packet.ParseFromString(payload)
                except Exception:
                    continue
                info[packet_id] = (packet.channel, packet.pki_encrypted, packet.decoded.reply_id)
            seen = await session.execute(
                select(PacketSeen.packet_id, PacketSeen.channel)
                .where(PacketSeen.packet_id.in_(ids))
                .distinct()
            )
            for packet_id, name in seen.all():
                reported.setdefault(packet_id, set()).add(name)
            for packet_id in ids:
                if packet_id not in info:
                    continue
                hash_ = info[packet_id][0]
                for name in reported.get(packet_id, ()):
                    if names.verifies(name, hash_):
                        await _learn_channel_name(session, hash_, name, relabel=False)
            await session.commit()

    if not info:
        return 0
    logger.info("Recomputing the channel of %d stored packets", len(info))

    labels = {
        packet_id: names.resolve(hash_, reported.get(packet_id, ()), pki=pki)
        for packet_id, (hash_, pki, _) in info.items()
    }
    # A reply on an ambiguous hash takes the channel of the message it answers.
    for packet_id, (hash_, pki, reply_id) in info.items():
        if reply_id and reply_id in labels and labels[packet_id] == unknown_label(hash_):
            original = (info[reply_id][0], labels[reply_id])
            labels[packet_id] = names.resolve(
                hash_, reported.get(packet_id, ()), pki=pki, original=original
            )

    ids = list(labels)
    for i in range(0, len(ids), batch_size):
        async with mqtt_database.async_session() as session:
            await session.execute(
                update(Packet),
                [
                    {
                        "id": packet_id,
                        "channel": labels[packet_id],
                        "channel_hash": info[packet_id][0],
                    }
                    for packet_id in ids[i : i + batch_size]
                ],
            )
            await session.commit()
    logger.info("Recomputed the channel of %d stored packets", len(ids))
    return len(ids)


async def load_gateway_cache():
    async with mqtt_database.async_session() as session:
        result = await session.execute(
            select(Node.node_id).where(Node.is_mqtt_gateway == True)  # noqa: E712
        )
        MQTT_GATEWAY_CACHE.update(result.scalars().all())
