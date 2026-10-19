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

**Unknown hop**:
A hop the firmware could not identify, recorded as a placeholder node number in the path.

**Gateway**:
A node that uplinks packets it hears to MQTT; each traceroute observation comes from one gateway.
_Avoid_: uplink node, MQTT node
