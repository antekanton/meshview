"""Main web server routes and page rendering for Meshview."""

import asyncio
import datetime
import itertools
import logging
import os
import pathlib
import re
import ssl
from dataclasses import dataclass

import pydot
from aiohttp import web
from google.protobuf import text_format
from google.protobuf.message import Message
from jinja2 import Environment, PackageLoader, Undefined, select_autoescape
from markupsafe import Markup

from meshtastic.protobuf.mesh_pb2 import Routing
from meshtastic.protobuf.portnums_pb2 import PortNum
from meshview import config, database, decode_payload, migrations, models, store
from meshview.__version__ import (
    __version_string__,
)
from meshview.deps import check_optional_deps
from meshview.web_api import api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(filename)s:%(lineno)d [pid:%(process)d] %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

logger = logging.getLogger(__name__)
SEQ_REGEX = re.compile(r"seq \d+")
SOFTWARE_RELEASE = __version_string__  # Keep for backward compatibility
CONFIG = config.CONFIG

env = Environment(loader=PackageLoader("meshview"), autoescape=select_autoescape())

# Start Database
database.init_database(CONFIG["database"]["connection_string"])
check_optional_deps()

BASE_DIR = os.path.dirname(__file__)
LANG_DIR = os.path.join(BASE_DIR, "lang")

with open(os.path.join(os.path.dirname(__file__), '1x1.png'), 'rb') as png:
    empty_png = png.read()


@dataclass
class Packet:
    """UI-friendly packet wrapper for templates and API payloads."""

    id: int
    packet_id: int | None
    from_node_id: int
    from_node: models.Node
    to_node_id: int
    to_node: models.Node
    channel: str
    portnum: int
    data: str
    raw_mesh_packet: object
    raw_payload: object
    payload: str
    pretty_payload: Markup
    import_time_us: int

    @classmethod
    def from_model(cls, packet):
        """Convert a Packet ORM model into a presentation-friendly Packet."""
        mesh_packet, payload = decode_payload.decode(packet)
        raw_payload = None
        if mesh_packet:
            if mesh_packet.HasField('encrypted'):
                raw_payload = mesh_packet.encrypted
            elif mesh_packet.HasField('decoded'):
                raw_payload = mesh_packet.decoded.SerializePartialToString()
        pretty_payload = None

        if mesh_packet:
            mesh_packet.decoded.payload = b""
            text_mesh_packet = text_format.MessageToString(mesh_packet)
        else:
            text_mesh_packet = "Did node decode"

        if payload is None:
            text_payload = "Did not decode"
        elif (
            packet.portnum == PortNum.ROUTING_APP
            and isinstance(payload, Routing)
            and payload.WhichOneof("variant") == "error_reason"
        ):
            # error_reason NONE is the implicit ACK, not a failure.
            text_payload = (
                "ACK"
                if payload.error_reason == Routing.Error.NONE
                else f"NAK: {Routing.Error.Name(payload.error_reason)}"
            )
        elif isinstance(payload, Message):
            text_payload = text_format.MessageToString(payload)
        elif packet.portnum == PortNum.TEXT_MESSAGE_APP and packet.to_node_id != 0xFFFFFFFF:
            text_payload = "<redacted>"
        elif isinstance(payload, bytes):
            text_payload = payload.decode("utf-8", errors="replace")  # decode bytes safely
        else:
            text_payload = str(payload)

        if payload:
            if (
                packet.portnum == PortNum.POSITION_APP
                and getattr(payload, "latitude_i", None)
                and getattr(payload, "longitude_i", None)
            ):
                pretty_payload = Markup(
                    f'<a href="https://www.google.com/maps/search/?api=1&query={payload.latitude_i * 1e-7},{payload.longitude_i * 1e-7}" target="_blank">map</a>'
                )

        packet_id = None
        if mesh_packet and mesh_packet.id:
            packet_id = mesh_packet.id

        return cls(
            id=packet.id,
            packet_id=packet_id,
            from_node=packet.from_node,
            from_node_id=packet.from_node_id,
            to_node=packet.to_node,
            to_node_id=packet.to_node_id,
            channel=packet.channel,
            portnum=packet.portnum,
            data=text_mesh_packet,
            payload=text_payload,  # now always a string
            pretty_payload=pretty_payload,
            import_time_us=packet.import_time_us,  # <-- include microseconds
            raw_mesh_packet=mesh_packet,
            raw_payload=raw_payload,
        )


async def build_trace(node_id):
    """Build a recent GPS trace list for a node using position packets."""
    trace = []
    for raw_p in await store.get_packets_from(
        node_id, PortNum.POSITION_APP, since=datetime.timedelta(hours=24)
    ):
        p = Packet.from_model(raw_p)
        if not p.raw_payload or not p.raw_payload.latitude_i or not p.raw_payload.longitude_i:
            continue
        trace.append((p.raw_payload.latitude_i * 1e-7, p.raw_payload.longitude_i * 1e-7))

    if not trace:
        for raw_p in await store.get_packets_from(node_id, PortNum.POSITION_APP):
            p = Packet.from_model(raw_p)
            if not p.raw_payload or not p.raw_payload.latitude_i or not p.raw_payload.longitude_i:
                continue
            trace.append((p.raw_payload.latitude_i * 1e-7, p.raw_payload.longitude_i * 1e-7))
            break

    return trace


async def build_neighbors(node_id):
    """Return neighbor node metadata for the given node ID."""
    packets = await store.get_packets_from(node_id, PortNum.NEIGHBORINFO_APP, limit=1)
    packet = packets.first()

    if not packet:
        return []

    _, payload = decode_payload.decode(packet)
    neighbors = {}

    # Gather node information asynchronously
    tasks = {n.node_id: store.get_node(n.node_id) for n in payload.neighbors}
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)

    for neighbor, node in zip(payload.neighbors, results, strict=False):
        if isinstance(node, Exception):
            continue
        if node and node.last_lat and node.last_long:
            neighbors[neighbor.node_id] = {
                'node_id': neighbor.node_id,
                'snr': neighbor.snr,  # Fix dictionary keying issue
                'short_name': node.short_name,
                'long_name': node.long_name,
                'location': (node.last_lat * 1e-7, node.last_long * 1e-7),
            }

    return list(neighbors.values())  # Return a list of dictionaries


def node_id_to_hex(node_id):
    """Format a node_id in Meshtastic hex notation."""
    if node_id is None or isinstance(node_id, Undefined):
        return "Invalid node_id"  # i... have no clue
    if node_id == 4294967295:
        return "^all"
    else:
        return f"!{hex(node_id)[2:].zfill(8)}"


def format_timestamp(timestamp):
    """Normalize timestamps to ISO 8601 strings."""
    if isinstance(timestamp, int):
        timestamp = datetime.datetime.fromtimestamp(timestamp, datetime.UTC)
    return timestamp.isoformat(timespec="milliseconds")


env.filters["node_id_to_hex"] = node_id_to_hex
env.filters["format_timestamp"] = format_timestamp

# Initialize API module with dependencies
api.init_api_module(Packet, SEQ_REGEX, LANG_DIR)

# Create main routes table
routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    """
    Redirect root URL '/' to the page specified in CONFIG['site']['starting'].
    Defaults to '/map' if not set.
    """
    # Get the starting page from config
    starting_url = CONFIG["site"].get("starting", "/map")  # default to /map if not set
    raise web.HTTPFound(location=starting_url)


# redirect for backwards compatibility
@routes.get("/packet_list/{packet_id}")
async def redirect_packet_list(request):
    packet_id = request.match_info["packet_id"]
    raise web.HTTPFound(location=f"/node/{packet_id}")


# Generic static HTML route
@routes.get("/{page}")
async def serve_page(request):
    """Serve static HTML pages from meshview/static."""
    page = request.match_info["page"]

    # default to index.html if no extension
    if not page.endswith(".html"):
        page = f"{page}.html"

    html_file = pathlib.Path(__file__).parent / "static" / page
    if not html_file.exists():
        raise web.HTTPNotFound(text=f"Page '{page}' not found")

    content = html_file.read_text(encoding="utf-8")
    return web.Response(text=content, content_type="text/html")


@routes.get("/docs/{doc}")
async def serve_doc(request):
    """Serve documentation files from docs/ (markdown)."""
    doc = request.match_info["doc"]
    docs_root = pathlib.Path(__file__).parent.parent / "docs"
    doc_path = (docs_root / doc).resolve()

    if not doc_path.is_file() or docs_root not in doc_path.parents:
        raise web.HTTPNotFound(text="Document not found")

    content = doc_path.read_text(encoding="utf-8")
    return web.Response(text=content, content_type="text/markdown")


@routes.get("/net")
async def net(request):
    return web.Response(
        text=env.get_template("net.html").render(),
        content_type="text/html",
    )


@routes.get("/map")
async def map(request):
    template = env.get_template("map.html")
    return web.Response(text=template.render(), content_type="text/html")


@routes.get("/reliability")
async def reliability(request):
    template = env.get_template("reliability.html")
    return web.Response(text=template.render(), content_type="text/html")


@routes.get("/nodelist")
async def nodelist(request):
    template = env.get_template("nodelist.html")
    return web.Response(
        text=template.render(),
        content_type="text/html",
    )


@routes.get("/firehose")
async def firehose(request):
    return web.Response(
        text=env.get_template("firehose.html").render(),
        content_type="text/html",
    )


@routes.get("/chat")
async def chat(request):
    template = env.get_template("chat.html")
    return web.Response(
        text=template.render(),
        content_type="text/html",
    )


@routes.get("/packet/{packet_id}")
async def new_packet(request):
    template = env.get_template("packet.html")
    return web.Response(
        text=template.render(),
        content_type="text/html",
    )


@routes.get("/node/{from_node_id}")
async def firehose_node(request):
    template = env.get_template("node.html")
    return web.Response(
        text=template.render(),
        content_type="text/html",
    )


@routes.get("/nodegraph")
async def nodegraph(request):
    template = env.get_template("nodegraph.html")
    return web.Response(
        text=template.render(),
        content_type="text/html",
    )


@routes.get("/top")
async def top(request):
    template = env.get_template("top.html")
    return web.Response(
        text=template.render(),
        content_type="text/html",
    )


@routes.get("/stats")
async def stats(request):
    template = env.get_template("stats.html")
    return web.Response(
        text=template.render(),
        content_type="text/html",
    )


@routes.get("/traceroute/{packet_id}")
async def traceroute_page(request):
    template = env.get_template("traceroute.html")
    return web.Response(
        text=template.render(),
        content_type="text/html",
    )


def _gateway_hops(packet_seen):
    """Hops the packet had taken when the gateway heard it, or None if unknown.

    Old firmware leaves hop_start at 0, so that counts as unknown too.
    """
    if packet_seen is None:
        return None
    hop_start = packet_seen.hop_start
    hop_limit = packet_seen.hop_limit
    if not hop_start or hop_limit is None or hop_limit > hop_start:
        return None
    return hop_start - hop_limit


# Distinct, readable on white; reused in order when there are more paths than colours.
TRACEROUTE_PATH_COLORS = (
    "#1f77b4",
    "#d62728",
    "#2ca02c",
    "#ff7f0e",
    "#9467bd",
    "#8c564b",
    "#e377c2",
    "#17becf",
    "#7f7f7f",
    "#bcbd22",
    "#393b79",
    "#637939",
)
# Edges on a segment that several paths share.
TRACEROUTE_SHARED_EDGE_COLOR = "#888888"


def _short_node_name(nodes, node_id):
    node = nodes.get(node_id)
    hex_id = node_id_to_hex(node_id)
    if node and node.short_name:
        return f"{node.short_name} ({hex_id})"
    return hex_id


def _traceroute_legend():
    """Legend node explaining the node border styles of the traceroute graph."""
    rows = "".join(
        f'<TR><TD BORDER="1" WIDTH="22" HEIGHT="12" {attrs}></TD><TD ALIGN="LEFT">{name}</TD></TR>'
        for attrs, name in (
            ('BGCOLOR="#bbbbbb"', "target"),
            ("", "gateway"),
            ('STYLE="dashed"', "relay"),
        )
    )
    table = f'<TABLE BORDER="0" CELLBORDER="0" CELLSPACING="3">{rows}</TABLE>'
    return pydot.Node("legend", label=f"<{table}>", shape="plaintext", fontsize="11")


# Keep !!
@routes.get("/graph/traceroute/{packet_id}")
async def graph_traceroute(request):
    packet_id = int(request.match_info['packet_id'])
    traceroutes = list(await store.get_traceroute(packet_id))

    packet = await store.get_packet(packet_id)
    if not packet:
        return web.Response(
            status=404,
        )
    packets_seen = await store.get_packets_seen(packet_id)
    packets_seen_by_gateway_id = {packet_seen.node_id: packet_seen for packet_seen in packets_seen}

    node_ids = set()
    is_route_back = False
    for tr in traceroutes:
        route = decode_payload.decode_payload(PortNum.TRACEROUTE_APP, tr.route)
        if route is None:
            continue
        # Traceroute is done only in packet going through return route
        if tr.done:
            is_route_back = True
        for node_id in getattr(route, 'route_back', []):
            node_ids.add(node_id)
        node_ids.add(tr.gateway_node_id)
        for node_id in route.route:
            node_ids.add(node_id)

    node_ids.add(packet.from_node_id)
    node_ids.add(packet.to_node_id)

    nodes = {}
    async with asyncio.TaskGroup() as tg:
        for node_id in node_ids:
            nodes[node_id] = tg.create_task(store.get_node(node_id))

    graph = pydot.Dot('traceroute', graph_type="digraph")
    # Added first so graphviz places it in the top-left corner.
    graph.add_node(_traceroute_legend())

    found_paths = set()
    # Distinct SNR labels seen on each directed segment, in the order first seen.
    segment_snr = {}
    mqtt_nodes = set()
    dest = packet.to_node_id if is_route_back else None
    for tr in traceroutes:
        route = decode_payload.decode_payload(PortNum.TRACEROUTE_APP, tr.route)
        if route is None:
            continue
        is_received_by_destination = packet.to_node_id == tr.gateway_node_id
        path = [packet.from_node_id]
        gateway_snr = None
        direction_route = getattr(route, 'route_back' if is_route_back else 'route', [])
        direction_route_snr = getattr(route, 'snr_back' if is_route_back else 'snr_towards', [])
        path.extend(direction_route)
        if is_received_by_destination:
            dest = packet.to_node_id
            if path[-1] != tr.gateway_node_id:
                path.append(tr.gateway_node_id)
        # The route's SNR list covers every edge up to here only when the lengths match.
        route_snr = []
        if len(direction_route_snr) == len(path) - 1:
            route_snr = [f"{s / 4.0:.2f}dB" for s in direction_route_snr]
        if path[-1] != tr.gateway_node_id:
            # It seems some nodes add them self to the list before uplinking
            path.append(tr.gateway_node_id)
            if tr.gateway_node_id in packets_seen_by_gateway_id:
                gateway_snr = f"{packets_seen_by_gateway_id[tr.gateway_node_id].rx_snr:.2f}dB"

        mqtt_nodes.add(tr.gateway_node_id)
        # Per-edge SNR of this reception; None where unknown.
        edge_snr = route_snr + [None] * (len(path) - 1 - len(route_snr))
        if gateway_snr is not None:
            edge_snr[-1] = gateway_snr
        found_paths.add(tuple(path))
        for segment, value in zip(itertools.pairwise(path), edge_snr, strict=True):
            labels = segment_snr.setdefault(segment, [])
            if value is not None and value not in labels:
                labels.append(value)

    nodes = {node_id: await task for node_id, task in nodes.items()}

    # Each path ends at the gateway that reported it; order and colour paths by length
    # then gateway name so colours stay the same on every render.
    paths = sorted(found_paths, key=lambda p: (len(p), _short_node_name(nodes, p[-1]), p))
    path_color = {
        path: TRACEROUTE_PATH_COLORS[i % len(TRACEROUTE_PATH_COLORS)]
        for i, path in enumerate(paths)
    }
    node_color = {path[-1]: color for path, color in path_color.items()}

    used_nodes = set()
    for path in paths:
        used_nodes.update(path)

    for node_id in used_nodes:
        node = nodes[node_id]
        if not node:
            node_name = node_id_to_hex(node_id)
        else:
            node_name = (
                f'[{node.short_name}] {node.long_name}\n{node_id_to_hex(node_id)}\n{node.role}'
            )

        if node_id in mqtt_nodes and node_id != packet.from_node_id:
            hops = _gateway_hops(packets_seen_by_gateway_id.get(node_id))
            if hops is not None:
                node_name += f'\n hop: {hops}'

        style = 'dashed'
        if node_id == dest:
            style = 'filled'
        elif node_id in mqtt_nodes:
            style = 'solid'

        graph.add_node(
            pydot.Node(
                str(node_id),
                label=node_name,
                shape='box',
                color=node_color.get(node_id, 'black'),
                style=style,
                href=f"/node/{node_id}",
            )
        )
    # One edge per directed segment: in its path's colour when only one path uses it,
    # grey when several do.
    segment_paths = {}
    for path in paths:
        for segment in itertools.pairwise(path):
            segment_paths.setdefault(segment, []).append(path)
    for (src, dst), seg_paths in segment_paths.items():
        color = path_color[seg_paths[0]] if len(seg_paths) == 1 else TRACEROUTE_SHARED_EDGE_COLOR
        label = " / ".join(segment_snr[(src, dst)])
        graph.add_edge(pydot.Edge(src, dst, color=color, label=label))

    return web.Response(
        body=graph.create_svg(),
        content_type="image/svg+xml",
    )


async def run_server():
    """Start the aiohttp web server after migrations are complete."""
    # Wait for database migrations to complete before starting web server
    logger.info("Checking database schema status...")
    database_url = CONFIG["database"]["connection_string"]

    # Wait for migrations to complete (writer app responsibility)
    migration_ready = await migrations.wait_for_migrations(
        database.engine, database_url, max_retries=30, retry_delay=2
    )

    if not migration_ready:
        logger.error("Database schema is not up to date. Cannot start web server.")
        raise RuntimeError("Database schema version mismatch - migrations not complete")

    logger.info("Database schema verified - starting web server")

    app = web.Application()
    app.router.add_static("/static/", pathlib.Path(__file__).parent / "static")
    app.add_routes(api.routes)  # Add API routes
    app.add_routes(routes)  # Add main web routes

    # Check if access logging should be disabled
    enable_access_log = CONFIG.get("logging", {}).get("access_log", "False").lower() == "true"
    access_log_handler = None if not enable_access_log else logging.getLogger("aiohttp.access")

    runner = web.AppRunner(app, access_log=access_log_handler)
    await runner.setup()
    if CONFIG["server"]["tls_cert"]:
        ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        ssl_context.load_cert_chain(CONFIG["server"]["tls_cert"])
        logger.info(f"TLS enabled with certificate: {CONFIG['server']['tls_cert']}")
    else:
        ssl_context = None
        logger.info("TLS disabled")
    if host := CONFIG["server"]["bind"]:
        port = CONFIG["server"]["port"]
        protocol = "https" if ssl_context else "http"
        site = web.TCPSite(runner, host, port, ssl_context=ssl_context)
        await site.start()
        # Display localhost instead of wildcard addresses for usability
        display_host = "localhost" if host in ("0.0.0.0", "*", "::") else host
        logger.info(f"Web server started at {protocol}://{display_host}:{port}")
    while True:
        await asyncio.sleep(3600)  # sleep forever
