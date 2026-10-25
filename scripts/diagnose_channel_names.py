"""Read-only diagnosis for issue #16: do gateways agree on a packet's channel name?

Usage: ./env/bin/python scripts/diagnose_channel_names.py [--config config.ini] [--hours 24]

For packets received in the window it reports:
  1. how often the reported channel names of one packet's receptions disagree;
  2. which gateways most often report a name other than the stored packet.channel;
  3. what MeshPacket.channel (from the stored payload) holds: a channel hash or a slot index;
  4. per gateway, how often its reported name matches the channel hash
     (a gateway that relabels every packet with one name shows a low match rate);
  5. whether replies sit on the same channel as the message they answer;
  6. per gateway, how many of its receptions lack the ok_to_mqtt bit (sniffers ignore it;
     old firmware never sets it, so the number is a hint, not proof).

The channel hash follows the firmware: xor of the name bytes ^ xor of the expanded PSK bytes,
with an empty name replaced by the modem preset name. Keys come from the config (default key
plus mqtt.secondary_keys); the key that decrypted a packet is not stored, so any key counts.
"""

import argparse
import asyncio
import sys
import time
from collections import Counter, defaultdict
from functools import reduce
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from meshtastic.protobuf.mesh_pb2 import MeshPacket  # noqa: E402
from meshview import database  # noqa: E402
from meshview.config import CONFIG  # noqa: E402
from meshview.models import Packet, PacketSeen  # noqa: E402
from meshview.mqtt_reader import PRIMARY_KEY, SECONDARY_KEYS  # noqa: E402

PRESET_NAMES = [
    "LongFast",
    "LongSlow",
    "LongModerate",
    "LongTurbo",
    "MediumSlow",
    "MediumFast",
    "ShortSlow",
    "ShortFast",
    "ShortTurbo",
    "VeryLongSlow",
]


def xor_hash(data: bytes) -> int:
    return reduce(lambda acc, b: acc ^ b, data, 0)


def channel_hash(name: str, key: bytes) -> int:
    return xor_hash(name.encode()) ^ xor_hash(key)


def _parse(payload):
    mp = MeshPacket()
    try:
        mp.ParseFromString(payload or b"")
    except Exception:
        return None
    return mp


def _air_channel(payload):
    mp = _parse(payload)
    return mp.channel if mp else None


def _reply_ids(payloads):
    for payload in payloads.values():
        mp = _parse(payload)
        if mp and mp.decoded.reply_id:
            yield mp.decoded.reply_id


def top(counter, n=20):
    return counter.most_common(n)


async def main(hours: int) -> None:
    database.init_database(CONFIG["database"]["connection_string"])
    since_us = int((time.time() - hours * 3600) * 1_000_000)
    keys = [PRIMARY_KEY, *SECONDARY_KEYS]

    async with database.async_session() as session:
        seen_rows = (
            await session.execute(
                select(
                    PacketSeen.packet_id,
                    PacketSeen.node_id,
                    PacketSeen.channel,
                    Packet.channel,
                    Packet.from_node_id,
                )
                .join(Packet, Packet.id == PacketSeen.packet_id)
                .where(PacketSeen.import_time_us >= since_us)
            )
        ).all()
        packet_ids = {r[0] for r in seen_rows}
        payloads = {}
        ids = list(packet_ids)
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            for pid, payload in (
                await session.execute(select(Packet.id, Packet.payload).where(Packet.id.in_(chunk)))
            ).all():
                payloads[pid] = payload

        # Originals of replies, whenever they were received
        originals = {}
        wanted = list({p for p in _reply_ids(payloads)})
        for i in range(0, len(wanted), 500):
            chunk = wanted[i : i + 500]
            for pid, channel, payload in (
                await session.execute(
                    select(Packet.id, Packet.channel, Packet.payload).where(Packet.id.in_(chunk))
                )
            ).all():
                originals[pid] = (channel, _air_channel(payload))

    # MeshPacket.channel per packet (from the first gateway's envelope)
    air_channel = {}
    reply_to = {}
    ok_to_mqtt = {}
    for pid, payload in payloads.items():
        if not payload:
            continue
        mp = MeshPacket()
        try:
            mp.ParseFromString(payload)
        except Exception:
            continue
        air_channel[pid] = mp.channel
        ok_to_mqtt[pid] = bool(mp.decoded.bitfield & 1)
        if mp.decoded.reply_id:
            reply_to[pid] = mp.decoded.reply_id

    names_by_packet = defaultdict(set)
    stored = {}
    for pid, _gw, reported, stored_name, _sender in seen_rows:
        names_by_packet[pid].add(reported)
        stored[pid] = stored_name

    all_names = {n for names in names_by_packet.values() for n in names if n} | set(PRESET_NAMES)
    hash_to_names = defaultdict(set)
    for name in all_names:
        for key in keys:
            hash_to_names[channel_hash(name, key)].add(name)

    print(f"Window: last {hours}h, receptions: {len(seen_rows)}, packets: {len(packet_ids)}")
    print(f"Keys in keyring: {len(keys)}")

    # 1. Disagreement
    disagree = {pid for pid, names in names_by_packet.items() if len(names) > 1}
    share = len(disagree) / len(names_by_packet) if names_by_packet else 0
    print(f"\n[1] Packets whose receptions disagree on the name: {len(disagree)} ({share:.1%})")
    combos = Counter(
        (stored[pid], tuple(sorted(n or "" for n in names_by_packet[pid]))) for pid in disagree
    )
    for (shown, names), count in top(combos):
        print(f"    {count:6d}  shown={shown!r}  reported={list(names)}")

    # 2. Gateways reporting a name other than the stored one
    odd = Counter(
        (gw, reported) for pid, gw, reported, stored_name, _ in seen_rows if reported != stored_name
    )
    print("\n[2] Gateways reporting a name other than packet.channel (gateway, name, receptions)")
    for (gw, name), count in top(odd):
        print(f"    !{gw:08x}  {name!r:20}  {count}")

    # 3. What MeshPacket.channel holds
    values = Counter(air_channel.values())
    small = sum(c for v, c in values.items() if v <= 7)
    explained = sum(c for v, c in values.items() if v in hash_to_names)
    total = sum(values.values())
    print(f"\n[3] MeshPacket.channel over {total} packets")
    print(f"    values 0..7 (could be a slot index): {small}")
    print(f"    values matching a known name+key hash: {explained}")
    for value, count in top(values, 15):
        print(f"    {value:3d}  {count:6d}  candidates={sorted(hash_to_names.get(value, []))}")

    # 4. Per gateway: does its reported name match the hash?
    per_gw = defaultdict(Counter)
    for pid, gw, reported, _stored, _ in seen_rows:
        if pid not in air_channel:
            continue
        matches = reported in hash_to_names.get(air_channel[pid], set())
        per_gw[gw]["match" if matches else "miss"] += 1
        per_gw[gw][f"name:{reported}"] += 1
    print("\n[4] Gateways with the lowest hash match rate (min 20 receptions)")
    rows = []
    for gw, c in per_gw.items():
        n = c["match"] + c["miss"]
        if n >= 20:
            names = [k[5:] for k, _ in c.most_common() if k.startswith("name:")][:3]
            rows.append((c["match"] / n, n, gw, names))
    for rate, n, gw, names in sorted(rows)[:25]:
        print(f"    !{gw:08x}  match={rate:5.1%}  receptions={n:6d}  names={names}")

    # 5. Replies vs the message they answer
    same_name = same_hash = missing = 0
    mismatches = Counter()
    for pid, orig_id in reply_to.items():
        if orig_id not in originals:
            missing += 1
            continue
        orig_name, orig_hash = originals[orig_id]
        same_name += stored.get(pid) == orig_name
        same_hash += air_channel.get(pid) == orig_hash
        if stored.get(pid) != orig_name:
            mismatches[(stored.get(pid), orig_name)] += 1
    found = len(reply_to) - missing
    print(f"\n[5] Replies: {len(reply_to)}, original not in DB: {missing}")
    if found:
        print(f"    same packet.channel as original: {same_name}/{found}")
        print(f"    same MeshPacket.channel as original: {same_hash}/{found}")
        for (reply_name, orig_name), count in top(mismatches, 10):
            print(f"    {count:6d}  reply={reply_name!r}  original={orig_name!r}")

    # 6. ok_to_mqtt per gateway
    no_flag = Counter()
    total_gw = Counter()
    for pid, gw, _reported, _stored, sender in seen_rows:
        if pid not in ok_to_mqtt or gw == sender:
            continue
        total_gw[gw] += 1
        no_flag[gw] += not ok_to_mqtt[pid]
    print("\n[6] Gateways uplinking the most packets without ok_to_mqtt (min 20 receptions)")
    rows = [(no_flag[gw] / n, n, gw) for gw, n in total_gw.items() if n >= 20]
    for rate, n, gw in sorted(rows, reverse=True)[:25]:
        print(f"    !{gw:08x}  without_flag={rate:5.1%}  receptions={n}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=24)
    parser.add_argument("--config", default="config.ini")
    args, _ = parser.parse_known_args()
    asyncio.run(main(args.hours))
