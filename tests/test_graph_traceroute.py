"""graph_traceroute draws the forward path for a traceroute request and the
return path for a traceroute response, labelling edges with SNR and gateway
nodes with the hop count at which they heard the packet."""

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


def _seen(node_id, rx_snr, hop_start=None, hop_limit=None):
    return SimpleNamespace(node_id=node_id, rx_snr=rx_snr, hop_start=hop_start, hop_limit=hop_limit)


async def _render_graph(monkeypatch, packet, traceroutes, packets_seen):
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
    return rendered["graph"]


async def _render(monkeypatch, packet, traceroutes, packets_seen):
    graph = await _render_graph(monkeypatch, packet, traceroutes, packets_seen)
    return {
        (int(e.get_source()), int(e.get_destination())): (e.get("label") or "").strip('"')
        for e in graph.get_edges()
    }


async def _node_labels(monkeypatch, packet, traceroutes, packets_seen):
    graph = await _render_graph(monkeypatch, packet, traceroutes, packets_seen)
    return {
        int(n.get_name()): (n.get("label") or "").strip('"')
        for n in graph.get_nodes()
        if n.get_name() != "legend"
    }


def _legend_label(graph):
    (legend,) = [n for n in graph.get_nodes() if n.get_name() == "legend"]
    return legend.get("label")


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
    packets_seen = [_seen(GATEWAY, 7.25)]

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
    packets_seen = [_seen(GATEWAY, -2.5)]

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


def _request_heard_by_gateway():
    packet = SimpleNamespace(from_node_id=INITIATOR, to_node_id=TARGET)
    traceroutes = [
        SimpleNamespace(
            route=_route(route=[HOP_TOWARDS], snr_towards=[12]),
            done=False,
            gateway_node_id=GATEWAY,
            import_time_us=1,
        )
    ]
    return packet, traceroutes


@pytest.mark.asyncio
async def test_gateway_node_shows_hop_count(monkeypatch):
    packet, traceroutes = _request_heard_by_gateway()
    packets_seen = [_seen(GATEWAY, 7.25, hop_start=3, hop_limit=1)]

    labels = await _node_labels(monkeypatch, packet, traceroutes, packets_seen)

    assert "hop: 2" in labels[GATEWAY]
    assert "hop:" not in labels[INITIATOR]
    assert "hop:" not in labels[HOP_TOWARDS]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("hop_start", "hop_limit"),
    [(None, 1), (3, None), (0, 0), (None, None), (1, 3)],
)
async def test_gateway_node_without_hop_data_has_no_hop_label(monkeypatch, hop_start, hop_limit):
    packet, traceroutes = _request_heard_by_gateway()
    packets_seen = [_seen(GATEWAY, 7.25, hop_start=hop_start, hop_limit=hop_limit)]

    labels = await _node_labels(monkeypatch, packet, traceroutes, packets_seen)

    assert "hop:" not in labels[GATEWAY]


@pytest.mark.asyncio
async def test_initiator_heard_as_gateway_has_no_hop_label(monkeypatch):
    # The packet's own sender uplinking it is not a hop-counted gateway.
    packet = SimpleNamespace(from_node_id=INITIATOR, to_node_id=TARGET)
    traceroutes = [
        SimpleNamespace(
            route=_route(route=[], snr_towards=[]),
            done=False,
            gateway_node_id=INITIATOR,
            import_time_us=1,
        ),
        SimpleNamespace(
            route=_route(route=[HOP_TOWARDS], snr_towards=[12]),
            done=False,
            gateway_node_id=GATEWAY,
            import_time_us=2,
        ),
    ]
    packets_seen = [
        _seen(INITIATOR, 9.0, hop_start=3, hop_limit=3),
        _seen(GATEWAY, 7.25, hop_start=3, hop_limit=2),
    ]

    labels = await _node_labels(monkeypatch, packet, traceroutes, packets_seen)

    assert "hop:" not in labels[INITIATOR]
    assert "hop: 1" in labels[GATEWAY]


def _two_gateways_sharing_a_hop():
    # Both gateways heard the request after HOP_TOWARDS, so INITIATOR -> HOP_TOWARDS is shared.
    packet = SimpleNamespace(from_node_id=INITIATOR, to_node_id=TARGET)
    traceroutes = [
        SimpleNamespace(
            route=_route(route=[HOP_TOWARDS], snr_towards=[12]),
            done=False,
            gateway_node_id=GATEWAY,
            import_time_us=1,
        ),
        SimpleNamespace(
            route=_route(route=[HOP_TOWARDS, HOP_BACK], snr_towards=[16, 20, 24]),
            done=False,
            gateway_node_id=TARGET,
            import_time_us=2,
        ),
    ]
    packets_seen = [_seen(GATEWAY, 7.25)]
    return packet, traceroutes, packets_seen


@pytest.mark.asyncio
async def test_shared_segment_gets_one_edge_per_path(monkeypatch):
    graph = await _render_graph(monkeypatch, *_two_gateways_sharing_a_hop())

    shared = [
        e
        for e in graph.get_edges()
        if (int(e.get_source()), int(e.get_destination())) == (INITIATOR, HOP_TOWARDS)
    ]

    assert len(shared) == 2
    assert {e.get("color") for e in shared} == set(web.TRACEROUTE_PATH_COLORS[:2])
    assert {e.get("label").strip('"') for e in shared} == {"3.00dB", "4.00dB"}


@pytest.mark.asyncio
async def test_legend_names_border_styles_only(monkeypatch):
    graph = await _render_graph(monkeypatch, *_two_gateways_sharing_a_hop())
    label = _legend_label(graph)

    assert [name for name in ("target", "gateway", "relay") if f">{name}</TD>" in label] == [
        "target",
        "gateway",
        "relay",
    ]
    assert "!0000000" not in label
