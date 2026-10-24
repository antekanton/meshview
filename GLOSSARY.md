# Meshview

Monitoring and diagnostics for Meshtastic mesh networks, built from packets observed via MQTT.

## Traceroute

**Initiator**:
The node that started a traceroute.
_Avoid_: source, origin, sender (the packet's `from` is not always the initiator)

**Target**:
The node being traced; it answers the traceroute.
_Avoid_: destination (the packet's `to` is not always the target)

**Traceroute request**:
The traceroute packet travelling from the initiator towards the target (`want_response` set). Its `from` is the initiator and `to` is the target.

**Traceroute response**:
The traceroute packet sent back by the target (`done`). Its endpoints are reversed: `from` is the target and `to` is the initiator.
_Avoid_: reply, answer

**Forward path**:
The hops a traceroute took from the initiator to the target.
_Avoid_: route towards, outbound route

**Return path**:
The hops a traceroute response took from the target back towards the initiator; it is often still incomplete when a gateway reports it.
_Avoid_: reverse route, route back, back path

**Relay**:
A node a traceroute passed through, other than the initiator, the target and the gateway that reported the reception. The same node can be a relay in one reception and the gateway in another.
_Avoid_: intermediate node, hop node

**Unknown hop**:
A hop the firmware could not identify, recorded as a placeholder node number in the path.

**Gateway**:
A node that uplinks packets it hears to MQTT; each traceroute reception comes from one gateway.
_Avoid_: uplink node, MQTT node

## Packet reception

**Channel**:
The channel a packet was sent on, as configured on the sender's radio: a name together with an encryption key. A packet has exactly one channel, whatever the gateways that heard it call it.
_Avoid_: channel name (a name alone does not identify a channel), channel index

**Channel hash**:
A one-byte fingerprint of a channel's name and key that the sender puts on every packet. Different channels can share a hash, so it narrows a packet's channel down rather than naming it.

**Reported channel name**:
The channel name a gateway attaches to one reception, taken from that gateway's own settings. It is evidence of the packet's channel, not the channel itself, and can be wrong.
_Avoid_: packet channel, gateway channel

**Unknown channel**:
A channel whose name could not be established from the receptions; it is told apart from other unknown channels only by its channel hash. Reach of its packets is read against the gateways of the same unknown channel.
_Avoid_: default channel, no channel

**Reception**:
One instance of a gateway hearing a packet and uplinking it; the same gateway may produce several receptions of one packet (e.g. directly and via a relay).
_Avoid_: seen, observation, sighting

**Sniffer**:
A gateway that deliberately uplinks every packet it hears on the air, whatever its channel and whether or not the sender allowed MQTT, so that all traffic is visible; it gives them all one fixed reported channel name. Its receptions count towards reach; its reported channel name says nothing about the packet's channel.
_Avoid_: monitor, listener

**Reach**:
The number of distinct gateways that heard a packet, regardless of how many receptions each produced. The sender counts when it uplinked its own packet.
_Avoid_: seen count, seen by, reception count

**Active gateway**:
A gateway that, within the configured activity window (2 hours by default), uplinked at least one packet sent by another node on a given channel. A packet's reach is read against the number of active gateways on its channel; a sender that uplinked its own packet is added to that number for that packet only, so a gateway that only uplinks its own packets never counts for anyone else's.
_Avoid_: online gateway, known gateway
