# A packet's channel is identified by its channel hash, not by a gateway's reported name

Each gateway names a packet's channel from its own settings, and sniffers give every packet one fixed name. So taking the first gateway's name (the original behaviour) or a majority of gateways' names mislabels packets. On production data (24h, issue #16) 140 of 316 packets on `Ping` were stored as `MediumFast`, and only 159 of 297 replies shared the stored channel of the message they answer. The channel hash in `MeshPacket.channel` matched the original in 297 of 297 replies. We therefore identify a packet's channel as the name + key whose hash equals `MeshPacket.channel`. A reported name counts only when it passes that check, and verified names are remembered per hash. If no name is known, the packet goes to an unknown channel keyed by its hash.

## Considered Options

- **First report wins**: the original behaviour. A single sniffer relabels every packet it reports first.
- **Majority vote over reported names**: fails on channels heard mostly by sniffers, which is exactly where it matters.
- **Channel hash, with verified names** (chosen): needs the keyring, and needs a stored hash → name dictionary so names survive restarts.

## Consequences

- Modem preset names are candidates only with the default key. Otherwise a private channel can collide with a preset (`ShortTurbo` with key `AT4=` has hash 49, the same as an unnamed private channel seen in production).
- Channels are told apart by name + hash, so two channels with the same name and different keys are distinct.
- PKI direct messages carry hash 0. They are labelled `PKI` and are not treated as a channel.
- A packet's channel can change as more receptions arrive, and history is recomputed once when this ships.
