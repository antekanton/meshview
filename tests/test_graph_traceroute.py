"""graph_traceroute draws the forward path for a traceroute request and the
return path for a traceroute response, labelling edges with SNR."""

from types import SimpleNamespace

import pydot
import pytest

from meshtastic.protobuf.mesh_pb2 import RouteDiscovery
from meshview import store, web

INITIATOR = 1
HOP_TOWARDS = 2
HOP_BACK = 3
TARGET = 4
GATEWAY = 5


def _route(**fields):
    return RouteDiscovery(**fields).SerializeToString()


async def _render(monkeypatch, packet, traceroutes, packets_seen):
    async def get_traceroute(packet_id):
        return traceroutes

    async def get_packet(packet_id):
        return packet

    async def get_packets_seen(packet_id):
        return packets_seen

    async def get_node(node_id):
        return None

    monkeypatch.setattr(store, "get_traceroute", get_traceroute)
    monkeypatch.setattr(store, "get_packet", get_packet)
    monkeypatch.setattr(store, "get_packets_seen", get_packets_seen)
    monkeypatch.setattr(store, "get_node", get_node)

    rendered = {}

    def create_svg(self):
        rendered["graph"] = self
        return b"<svg/>"

    monkeypatch.setattr(pydot.Dot, "create_svg", create_svg)

    request = SimpleNamespace(match_info={"packet_id": "42"})
    response = await web.graph_traceroute(request)
    assert response.status == 200

    return {
        (int(e.get_source()), int(e.get_destination())): (e.get("label") or "").strip('"')
        for e in rendered["graph"].get_edges()
    }


@pytest.mark.asyncio
async def test_request_draws_forward_path_to_gateway(monkeypatch):
    packet = SimpleNamespace(from_node_id=INITIATOR, to_node_id=TARGET)
    traceroutes = [
        SimpleNamespace(
            route=_route(route=[HOP_TOWARDS], snr_towards=[12]),
            done=False,
            gateway_node_id=GATEWAY,
            import_time_us=1,
        )
    ]
    packets_seen = [SimpleNamespace(node_id=GATEWAY, rx_snr=7.25)]

    edges = await _render(monkeypatch, packet, traceroutes, packets_seen)

    assert edges == {
        (INITIATOR, HOP_TOWARDS): "3.00dB",
        (HOP_TOWARDS, GATEWAY): "7.25dB",
    }


@pytest.mark.asyncio
async def test_response_draws_return_path_from_target(monkeypatch):
    # A traceroute response travels from the target back to the initiator.
    packet = SimpleNamespace(from_node_id=TARGET, to_node_id=INITIATOR)
    traceroutes = [
        SimpleNamespace(
            route=_route(
                route=[HOP_TOWARDS],
                snr_towards=[12, 16],
                route_back=[HOP_BACK],
                snr_back=[20],
            ),
            done=True,
            gateway_node_id=GATEWAY,
            import_time_us=1,
        )
    ]
    packets_seen = [SimpleNamespace(node_id=GATEWAY, rx_snr=-2.5)]

    edges = await _render(monkeypatch, packet, traceroutes, packets_seen)

    assert edges == {
        (TARGET, HOP_BACK): "5.00dB",
        (HOP_BACK, GATEWAY): "-2.50dB",
    }


@pytest.mark.asyncio
async def test_response_heard_by_initiator_ends_at_initiator(monkeypatch):
    packet = SimpleNamespace(from_node_id=TARGET, to_node_id=INITIATOR)
    traceroutes = [
        SimpleNamespace(
            route=_route(
                route=[HOP_TOWARDS],
                snr_towards=[12, 16],
                route_back=[HOP_BACK],
                snr_back=[20, 24],
            ),
            done=True,
            gateway_node_id=INITIATOR,
            import_time_us=1,
        )
    ]

    edges = await _render(monkeypatch, packet, traceroutes, [])

    assert edges == {
        (TARGET, HOP_BACK): "5.00dB",
        (HOP_BACK, INITIATOR): "6.00dB",
    }
