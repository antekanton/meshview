"""Which channel a packet was sent on (see GLOSSARY.md and docs/adr/0001).

A packet's channel is identified by the channel hash the sender put on it
(MeshPacket.channel). A gateway's reported channel name only counts when it is a
verified channel name: together with one of the known keys it produces that hash.

Every channel gets one label, stored in packet.channel:
  - its name, e.g. "Ping";
  - "<name> #<hash>" when the same name is verified for more than one hash;
  - "? #<hash>" for an unknown channel;
  - "PKI" for direct messages encrypted with the recipient's key (not a channel).
"""

import re
from functools import reduce

PKI_LABEL = "PKI"

# Modem preset names. A channel with an empty name is called by its preset name.
PRESET_NAMES = (
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
)

_UNKNOWN_LABEL = re.compile(r"^\? #(\d+)$")


def channel_hash(name: str, key: bytes) -> int:
    """Firmware channel hash: xor of the name bytes ^ xor of the expanded key bytes."""
    xor = lambda data: reduce(lambda acc, b: acc ^ b, data, 0)  # noqa: E731
    return xor(name.encode()) ^ xor(key)


def unknown_label(hash_: int) -> str:
    return f"? #{hash_}"


def is_unknown_label(label: str | None) -> bool:
    return bool(label and _UNKNOWN_LABEL.match(label))


class ChannelNames:
    """Verified channel names per channel hash, and the label each channel gets.

    Preset names are known in advance, but only with the default key: a preset name
    paired with any other key is far more likely a collision than a real channel.
    """

    def __init__(self, default_key: bytes, keys: list[bytes]):
        self.default_key = default_key
        self.keys = list(keys)
        self.names_by_hash: dict[int, set[str]] = {}
        self.hashes_by_name: dict[str, set[int]] = {}
        for preset in PRESET_NAMES:
            self._remember(channel_hash(preset, default_key), preset)

    def verifies(self, name: str | None, hash_: int) -> bool:
        if not name or name == PKI_LABEL:
            return False
        keys = [self.default_key] if name in PRESET_NAMES else self.keys
        return any(channel_hash(name, key) == hash_ for key in keys)

    def _remember(self, hash_: int, name: str) -> None:
        self.names_by_hash.setdefault(hash_, set()).add(name)
        self.hashes_by_name.setdefault(name, set()).add(hash_)

    def learn(self, hash_: int, name: str) -> bool:
        """Remember a verified name for a hash. Returns True when it was new."""
        if name in self.names_by_hash.get(hash_, set()):
            return False
        self._remember(hash_, name)
        return True

    def label(self, name: str, hash_: int) -> str:
        if len(self.hashes_by_name.get(name, ())) > 1:
            return f"{name} #{hash_}"
        return name

    def resolve(
        self,
        hash_: int,
        reported_names,
        pki: bool = False,
        original: tuple[int | None, str | None] | None = None,
    ) -> str:
        """Label of the channel a packet with this hash was sent on.

        reported_names: reported channel names of the packet's receptions.
        pki: the packet is a PKI direct message (only meaningful with hash 0).
        original: (hash, label) of the message this packet replies to, if any.
        """
        reported = set(reported_names)
        if hash_ == 0 and (pki or PKI_LABEL in reported):
            return PKI_LABEL

        verified_here = {name for name in reported if self.verifies(name, hash_)}
        candidates = verified_here or self.names_by_hash.get(hash_, set())
        if len(candidates) == 1:
            return self.label(next(iter(candidates)), hash_)

        if original is not None and original[0] == hash_:
            labels = {self.label(name, hash_) for name in candidates}
            if original[1] in labels:
                return original[1]

        return unknown_label(hash_)
