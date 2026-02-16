import json
import logging
import os
import uuid
import time
from collections import defaultdict

import numpy as np
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import create_engine, event, MetaData, Table, select, distinct, or_, text
from sqlalchemy.exc import OperationalError

from typing import List
from models import (
    AppImportMappedPort,
    AppImportPortByProtocol,
    AppImportServer,
    EnrichedPortResponse,
    MappedPort,
    MappedPortByProtocol,
    PortResponse,
    SemanticSearchRequest,
    SemanticSearchResponse,
    SemanticSearchResult,
    ServerTopology,
    ServiceMeta,
    TargetRequest,
    TopologyMetadata,
    TopologyRequest,
    TopologyResponse,
    PortRequest,
    SourceRequest,
    SourceResponse,
)

log = logging.getLogger(__name__)

app = FastAPI()

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=False,  # Disables credential sharing (default)
    allow_methods=["*"],  # Allows all HTTP methods
    allow_headers=["*"],  # Allows all headers
)

# SQLAlchemy setup
db_path = os.environ.get("DB_PATH", "allports_updated.db")
engine = create_engine(f"sqlite:///{db_path}", future=True)

# --- sqlite-vec extension loading (must be registered BEFORE any connections) ---
_vectors_available = False
try:
    import sqlite_vec

    @event.listens_for(engine, "connect")
    def _load_sqlite_vec(dbapi_conn, connection_record):
        try:
            dbapi_conn.enable_load_extension(True)
            sqlite_vec.load(dbapi_conn)
            dbapi_conn.enable_load_extension(False)
        except AttributeError:
            pass  # Python built without SQLITE_ENABLE_LOAD_EXTENSION (e.g. macOS)
except ImportError:
    pass

# Reflect tables (this opens a connection, which now has sqlite-vec loaded if available)
metadata = MetaData()
all_ports = Table("all_ports", metadata, autoload_with=engine)

# Probe for vector table existence
try:
    with engine.connect() as _conn:
        _conn.execute(text("SELECT COUNT(*) FROM port_embeddings"))
    _vectors_available = True
    log.info("sqlite-vec loaded, vector search enabled")
except OperationalError:
    log.info("Vector search not available (sqlite-vec or port_embeddings missing), using keyword fallback")


def clean_up_old_files(directory, min_age_minutes=1):
    current_time = time.time()
    files = os.listdir(directory)
    if len(files) > 1:
        for filename in files:
            file_path = os.path.join(directory, filename)
            if os.path.isfile(file_path):
                file_creation_time = os.path.getctime(file_path)
                file_age_minutes = (current_time - file_creation_time) / 60
                if file_age_minutes > min_age_minutes:
                    os.remove(file_path)


@app.get("/health")
async def health_check():
    return {"status": "healthy"}


@app.get("/")
async def get_services() -> List[str]:
    stmt = select(distinct(all_ports.c.product))
    with engine.connect() as conn:
        result = conn.execute(stmt)
        services = [row[0] for row in result]
    return services


@app.post("/source")
async def get_product(request: SourceRequest):
    product_name = request.productName
    stmt = select(distinct(all_ports.c.sourceService)).where(
        all_ports.c.product == product_name
    )
    with engine.connect() as conn:
        result = conn.execute(stmt)
        products = [row[0] for row in result]
    return products


@app.post("/sourceDetails", response_model=List[SourceResponse])
async def get_source_details(request: SourceRequest):
    product_name = request.productName
    stmt = (
        select(all_ports.c.product, all_ports.c.sourceService, all_ports.c.subheading)
        .distinct()
        .where(all_ports.c.product == product_name)
        .order_by(all_ports.c.subheading, all_ports.c.sourceService)
    )
    df = pd.read_sql(stmt, engine)
    records = df.to_dict("records")
    responses: SourceResponse = []
    for i in records:
        responses.append(
            SourceResponse(
                product=i["product"],
                sourceService=i["sourceService"],
                subheading=i["subheading"],
            )
        )

    return responses


@app.post("/target")
async def get_target(request: TargetRequest):
    source_service = request.sourceService
    product_name = request.productName
    stmt = select(distinct(all_ports.c.targetService)).where(
        (all_ports.c.sourceService == source_service)
        & (all_ports.c.product == product_name)
    )
    with engine.connect() as conn:
        result = conn.execute(stmt)
        to_ports = [row[0] for row in result]
    return to_ports


@app.post("/allTarget", response_model=List[PortResponse])
async def get_all_target(request: TargetRequest):
    source_service = request.sourceService
    product_name = request.productName
    subheading = request.subheading
    stmt = (
        select(all_ports)
        .where(
            (all_ports.c.sourceService == source_service)
            & (all_ports.c.product == product_name)
            & (all_ports.c.subheading == subheading)
        )
        .order_by(
            all_ports.c.subheading,
            all_ports.c.subheadingL2,
            all_ports.c.subheadingL3,
            all_ports.c.targetService,
        )
    )
    df = pd.read_sql(stmt, engine)
    records = df.to_dict("records")
    response_models = [PortResponse(**record) for record in records]
    return response_models


@app.post("/ports", response_model=List[PortResponse])
async def get_ports(request: PortRequest):
    source_service = request.sourceService
    target_service = request.targetService
    product_name = request.productName
    stmt = select(all_ports).where(
        (all_ports.c.sourceService == source_service)
        & (all_ports.c.targetService == target_service)
        & (all_ports.c.product == product_name)
    )
    df = pd.read_sql(stmt, engine)
    records = df.to_dict("records")
    response_models = [PortResponse(**record) for record in records]
    return response_models


@app.get("/products/{name}/ports", response_model=List[PortResponse])
async def get_product_ports(name: str):
    stmt = (
        select(all_ports)
        .where(all_ports.c.product == name)
        .order_by(
            all_ports.c.subheading,
            all_ports.c.subheadingL2,
            all_ports.c.subheadingL3,
        )
    )
    df = pd.read_sql(stmt, engine)
    records = df.to_dict("records")
    return [PortResponse(**record) for record in records]


@app.get("/products/{name}/subheadings")
async def get_product_subheadings(name: str) -> List[str]:
    stmt = (
        select(distinct(all_ports.c.subheading))
        .where(all_ports.c.product == name)
        .order_by(all_ports.c.subheading)
    )
    with engine.connect() as conn:
        result = conn.execute(stmt)
        return [row[0] for row in result]


def _parse_roles(raw: str) -> list[str]:
    """Parse a JSON-encoded list of roles, returning [] on failure."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def _build_service_metas(row) -> tuple[ServiceMeta, ServiceMeta]:
    """Build source and target ServiceMeta from an enriched_ports row."""
    source_meta = ServiceMeta(
        canonical=row["source_canonical"],
        roles=_parse_roles(row["source_roles"]),
        os=row["source_os"] or None,
        hypervisor=row["source_hypervisor"] or None,
        storage_type=row["source_storage_type"] or None,
        original=row["sourceService"],
    )
    target_meta = ServiceMeta(
        canonical=row["target_canonical"],
        roles=_parse_roles(row["target_roles"]),
        os=row["target_os"] or None,
        hypervisor=row["target_hypervisor"] or None,
        storage_type=row["target_storage_type"] or None,
        original=row["targetService"],
    )
    return source_meta, target_meta


@app.get("/products/{name}/enriched-ports", response_model=List[EnrichedPortResponse])
async def get_enriched_ports(name: str):
    query = text("""
        SELECT * FROM enriched_ports
        WHERE product = :product
        ORDER BY subheading, subheadingL2, subheadingL3
    """)
    try:
        with engine.connect() as conn:
            rows = conn.execute(query, {"product": name}).mappings().all()
    except OperationalError:
        raise HTTPException(
            status_code=404,
            detail="Enriched port data is not yet available. Run the scraper with enrichment enabled.",
        )

    if not rows:
        raise HTTPException(status_code=404, detail=f"No enriched data found for product '{name}'")

    results = []
    for row in rows:
        source_meta, target_meta = _build_service_metas(row)
        results.append(EnrichedPortResponse(
            subheading=row["subheading"],
            subheadingL2=row["subheadingL2"],
            subheadingL3=row["subheadingL3"],
            product=row["product"],
            sourceService=row["sourceService"],
            targetService=row["targetService"],
            protocol=row["protocol"],
            port=row["port"],
            description=row["description"],
            source_meta=source_meta,
            target_meta=target_meta,
        ))
    return results


def _resolve_topology_graph(name: str, request: TopologyRequest):
    """Shared resolution logic for topology and app-import endpoints.

    Returns (components, server_component_sets, component_to_servers,
             matched_connections, all_connections, enrichment_version, unresolved).
    """
    try:
        with engine.connect() as conn:
            components = _load_components(conn, name)
            if not components["all"]:
                raise HTTPException(
                    status_code=404,
                    detail=f"No graph data found for product '{name}'. Run the scraper with enrichment and graph construction enabled.",
                )

            variants = _load_variants(conn, name)
            enrichment_lookup = _load_enrichment_lookup(conn, name)
            all_connections = _load_connections(conn, name)
            enrichment_version = _get_enrichment_version(conn, name)
    except OperationalError:
        raise HTTPException(
            status_code=404,
            detail="Graph tables not found. Run the scraper without --skip-graph to build graph data.",
        )

    server_component_sets: dict[str, set[int]] = {}
    unresolved: list[str] = []

    for server in request.servers:
        component_ids: set[int] = set()
        for service_name in server.services:
            resolved = _resolve_service(service_name, components, enrichment_lookup)
            if resolved:
                component_ids.update(resolved)
            else:
                unresolved.append(service_name)
                log.warning("Could not resolve service '%s' for server '%s'", service_name, server.name)
        server_component_sets[server.name] = component_ids
        log.info("Server '%s': resolved %d component(s) from %d service(s)", server.name, len(component_ids), len(server.services))

    for server_name, comp_ids in server_component_sets.items():
        expanded = set(comp_ids)
        for cid in comp_ids:
            if cid in variants:
                expanded.add(variants[cid])
        server_component_sets[server_name] = expanded

    component_to_servers: dict[int, list[str]] = defaultdict(list)
    for server_name, comp_ids in server_component_sets.items():
        for cid in comp_ids:
            component_to_servers[cid].append(server_name)

    all_component_ids = set()
    for comp_ids in server_component_sets.values():
        all_component_ids.update(comp_ids)

    matched_connections = []
    for conn_row in all_connections:
        src_id, tgt_id = conn_row["source_component_id"], conn_row["target_component_id"]
        if src_id in all_component_ids and tgt_id in all_component_ids:
            matched_connections.append(conn_row)

    return (components, server_component_sets, component_to_servers,
            matched_connections, all_connections, enrichment_version, unresolved)


@app.post("/products/{name}/topology", response_model=TopologyResponse)
async def generate_topology(name: str, request: TopologyRequest):
    """Resolve server topology via knowledge graph traversal."""
    (components, server_component_sets, component_to_servers,
     matched_connections, all_connections, enrichment_version, unresolved) = \
        _resolve_topology_graph(name, request)

    server_topologies = []
    for server in request.servers:
        topology = _build_server_topology(
            server.name,
            server_component_sets[server.name],
            component_to_servers,
            matched_connections,
            request.options.include_loopback,
        )
        server_topologies.append(topology)

    total_product_connections = len(all_connections)
    total_matched = len(matched_connections)
    meta = TopologyMetadata(
        total_entries_matched=total_matched,
        total_entries_skipped=total_product_connections - total_matched,
        enrichment_version=enrichment_version,
        unresolved_services=unresolved if request.options.include_unresolved else [],
    )

    log.info(
        "Topology for '%s': %d connections matched, %d skipped, %d unresolved services",
        name, total_matched, total_product_connections - total_matched, len(unresolved),
    )

    return TopologyResponse(product=name, servers=server_topologies, metadata=meta)


@app.post("/products/{name}/app-import", response_model=List[AppImportServer])
async def generate_app_import(name: str, request: TopologyRequest):
    """Generate port mapping entries for Magic Ports frontend import."""
    (components, server_component_sets, component_to_servers,
     matched_connections, all_connections, _, _) = \
        _resolve_topology_graph(name, request)

    servers = []
    for server in request.servers:
        server_topology = _build_app_import_server(
            server.name,
            server_component_sets[server.name],
            component_to_servers,
            matched_connections,
            components["all"],
            name,
            request.options.include_loopback,
        )
        servers.append(server_topology)

    log.info(
        "App import for '%s': %d servers from %d matched connections",
        name, len(servers), len(matched_connections),
    )

    return servers


def _load_components(conn, product: str) -> dict:
    """Load all components for a product into memory for fast resolution."""
    rows = conn.execute(
        text("SELECT id, canonical, os, hypervisor, storage_type, original_name FROM components WHERE product = :p"),
        {"p": product},
    ).mappings().all()

    by_original: dict[str, list[int]] = defaultdict(list)
    by_original_lower: dict[str, list[int]] = defaultdict(list)
    by_key: dict[tuple, int] = {}
    by_canonical: dict[str, list[int]] = defaultdict(list)
    all_components: dict[int, dict] = {}

    for row in rows:
        cid = row["id"]
        orig = row["original_name"] or ""
        canonical = row["canonical"]
        os_val = row["os"] or None
        hyp = row["hypervisor"] or None
        storage = row["storage_type"] or None

        all_components[cid] = dict(row)
        by_original[orig].append(cid)
        by_original_lower[orig.lower()].append(cid)
        by_key[(canonical, os_val, hyp, storage)] = cid
        by_canonical[canonical].append(cid)

    return {
        "by_original": by_original,
        "by_original_lower": by_original_lower,
        "by_key": by_key,
        "by_canonical": by_canonical,
        "all": all_components,
    }


def _load_variants(conn, product: str) -> dict[int, int]:
    """Load variant links: specific_id → generic_id for a product."""
    rows = conn.execute(
        text("""
            SELECT cv.specific_id, cv.generic_id
            FROM component_variants cv
            JOIN components c ON cv.specific_id = c.id
            WHERE c.product = :p
        """),
        {"p": product},
    ).fetchall()

    return {row[0]: row[1] for row in rows}


def _load_enrichment_lookup(conn, product: str) -> dict[str, tuple]:
    """Map raw service names to (canonical, os, hypervisor, storage_type)."""
    rows = conn.execute(
        text("""
            SELECT DISTINCT sourceService, source_canonical, source_os, source_hypervisor, source_storage_type
            FROM enriched_ports WHERE product = :p
            UNION
            SELECT DISTINCT targetService, target_canonical, target_os, target_hypervisor, target_storage_type
            FROM enriched_ports WHERE product = :p
        """),
        {"p": product},
    ).fetchall()

    lookup: dict[str, tuple] = {}
    for row in rows:
        service_name = row[0]
        if service_name and service_name not in lookup:
            lookup[service_name] = (
                row[1],
                row[2] or None,
                row[3] or None,
                row[4] or None,
            )
    return lookup


def _load_connections(conn, product: str) -> list[dict]:
    """Load all connections for a product."""
    rows = conn.execute(
        text("""
            SELECT source_component_id, target_component_id, port, protocol, description
            FROM connections WHERE product = :p
        """),
        {"p": product},
    ).mappings().all()

    return [dict(r) for r in rows]


def _get_enrichment_version(conn, product: str) -> str | None:
    """Get the enrichment version for a product."""
    row = conn.execute(
        text("SELECT enrichment_version FROM enriched_ports WHERE product = :p LIMIT 1"),
        {"p": product},
    ).fetchone()
    return row[0] if row else None


def _resolve_service(
    service_name: str,
    components: dict,
    enrichment_lookup: dict[str, tuple],
) -> list[int]:
    """Resolve a user-provided service name to component IDs.

    Priority: exact original_name → case-insensitive original → enrichment lookup → canonical match.
    """
    # Priority 1: exact match on original_name
    if service_name in components["by_original"]:
        return components["by_original"][service_name]

    # Priority 2: case-insensitive match on original_name
    lower = service_name.lower()
    if lower in components["by_original_lower"]:
        return components["by_original_lower"][lower]

    # Priority 3: enrichment lookup (service name → canonical + qualifiers → component key)
    if service_name in enrichment_lookup:
        canonical, os_val, hyp, storage = enrichment_lookup[service_name]
        key = (canonical, os_val, hyp, storage)
        if key in components["by_key"]:
            return [components["by_key"][key]]

    # Priority 4: canonical name match — prefer fully-generic component to avoid variant cross-contamination
    if lower in components["by_canonical"]:
        generic_key = (lower, None, None, None)
        if generic_key in components["by_key"]:
            return [components["by_key"][generic_key]]
        return components["by_canonical"][lower]

    return []


def _build_server_topology(
    server_name: str,
    server_components: set[int],
    component_to_servers: dict[int, list[str]],
    connections: list[dict],
    include_loopback: bool,
) -> ServerTopology:
    """Build the topology response for a single server."""
    mapped_ports: list[MappedPort] = []
    seen: set[tuple] = set()  # dedupe on (peer_server, port, protocol, direction)

    for conn_row in connections:
        src_id = conn_row["source_component_id"]
        tgt_id = conn_row["target_component_id"]
        port = conn_row["port"]
        protocol = conn_row["protocol"]
        description = conn_row["description"] or ""

        # Outbound: this server is source, other server is target
        if src_id in server_components:
            for peer_server in component_to_servers.get(tgt_id, []):
                if peer_server == server_name and not include_loopback:
                    continue
                dedup_key = (peer_server, port, protocol, "outbound")
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    mapped_ports.append(MappedPort(
                        server=peer_server, port=port, protocol=protocol,
                        description=description, direction="outbound",
                    ))

        # Inbound: this server is target, other server is source
        if tgt_id in server_components:
            for peer_server in component_to_servers.get(src_id, []):
                if peer_server == server_name and not include_loopback:
                    continue
                dedup_key = (peer_server, port, protocol, "inbound")
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    mapped_ports.append(MappedPort(
                        server=peer_server, port=port, protocol=protocol,
                        description=description, direction="inbound",
                    ))

    # Aggregate protocol lists
    inbound_tcp, outbound_tcp = set(), set()
    inbound_udp, outbound_udp = set(), set()
    outbound_by_sp: dict[tuple, set] = defaultdict(set)
    inbound_by_sp: dict[tuple, set] = defaultdict(set)

    for mp in mapped_ports:
        protocols = _split_protocols(mp.protocol)
        for proto in protocols:
            if mp.direction == "outbound":
                if proto == "TCP":
                    outbound_tcp.add(mp.port)
                elif proto == "UDP":
                    outbound_udp.add(mp.port)
                outbound_by_sp[(mp.server, proto)].add(mp.port)
            else:
                if proto == "TCP":
                    inbound_tcp.add(mp.port)
                elif proto == "UDP":
                    inbound_udp.add(mp.port)
                inbound_by_sp[(mp.server, proto)].add(mp.port)

    mapped_by_protocol = [
        MappedPortByProtocol(server=s, protocol=p, ports=sorted(ports))
        for (s, p), ports in sorted(outbound_by_sp.items())
    ]
    mapped_by_protocol_inbound = [
        MappedPortByProtocol(server=s, protocol=p, ports=sorted(ports))
        for (s, p), ports in sorted(inbound_by_sp.items())
    ]

    peer_servers = set(mp.server for mp in mapped_ports if mp.server != server_name)
    inbound_count = sum(1 for mp in mapped_ports if mp.direction == "inbound")

    return ServerTopology(
        id=str(uuid.uuid4()),
        sourceServer=server_name,
        totalMappedPorts=len(mapped_ports),
        totalMappedInboundPorts=inbound_count,
        totalMappedServers=len(peer_servers),
        mappedPorts=mapped_ports,
        allInboundPortsTcp=sorted(inbound_tcp),
        allOutboundPortsTcp=sorted(outbound_tcp),
        allInboundPortsUdp=sorted(inbound_udp),
        allOutboundPortsUdp=sorted(outbound_udp),
        mappedPortsByProtocol=mapped_by_protocol,
        mappedPortsByProtocolInbound=mapped_by_protocol_inbound,
    )


def _split_protocols(protocol: str) -> list[str]:
    """Split a protocol string like 'TCP, UDP' into individual protocols."""
    parts = [p.strip().upper() for p in protocol.replace("/", ",").split(",")]
    return [p for p in parts if p in ("TCP", "UDP")]


def _build_app_import_server(
    server_name: str,
    server_components: set[int],
    component_to_servers: dict[int, list[str]],
    connections: list[dict],
    all_components: dict[int, dict],
    product: str,
    include_loopback: bool,
) -> AppImportServer:
    """Build the app-import server response for a single server.

    Similar to _build_server_topology but with corrected sub-structures
    matching the Magic Ports frontend import schema.
    """
    server_id = str(uuid.uuid4())
    mapped_ports: list[AppImportMappedPort] = []
    port_directions: list[str] = []  # track direction for aggregate grouping
    seen: set[tuple] = set()

    for conn_row in connections:
        src_id = conn_row["source_component_id"]
        tgt_id = conn_row["target_component_id"]
        port = conn_row["port"]
        protocol = conn_row["protocol"]
        description = conn_row["description"] or ""
        source_service = all_components[src_id]["original_name"]
        target_service = all_components[tgt_id]["original_name"]

        # Outbound: this server hosts the source component
        if src_id in server_components:
            for peer_server in component_to_servers.get(tgt_id, []):
                if peer_server == server_name and not include_loopback:
                    continue
                dedup_key = (peer_server, port, protocol, "outbound")
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    mapped_ports.append(AppImportMappedPort(
                        sourceServerId=server_id,
                        sourceServerName=server_name,
                        targetServerName=peer_server,
                        sourceService=source_service,
                        targetService=target_service,
                        description=description,
                        product=product,
                        port=port, protocol=protocol,
                    ))
                    port_directions.append("outbound")

        # Inbound: this server hosts the target component
        if tgt_id in server_components:
            for peer_server in component_to_servers.get(src_id, []):
                if peer_server == server_name and not include_loopback:
                    continue
                dedup_key = (peer_server, port, protocol, "inbound")
                if dedup_key not in seen:
                    seen.add(dedup_key)
                    mapped_ports.append(AppImportMappedPort(
                        sourceServerId=server_id,
                        sourceServerName=server_name,
                        targetServerName=peer_server,
                        sourceService=source_service,
                        targetService=target_service,
                        description=description,
                        product=product,
                        port=port, protocol=protocol,
                    ))
                    port_directions.append("inbound")

    # Aggregate protocol lists
    inbound_tcp, outbound_tcp = set(), set()
    inbound_udp, outbound_udp = set(), set()
    outbound_by_sp: dict[tuple, set] = defaultdict(set)
    inbound_by_sp: dict[tuple, set] = defaultdict(set)

    for mp, direction in zip(mapped_ports, port_directions):
        protocols = _split_protocols(mp.protocol)
        for proto in protocols:
            if direction == "outbound":
                if proto == "TCP":
                    outbound_tcp.add(mp.port)
                elif proto == "UDP":
                    outbound_udp.add(mp.port)
                outbound_by_sp[(mp.targetServerName, proto)].add(mp.port)
            else:
                if proto == "TCP":
                    inbound_tcp.add(mp.port)
                elif proto == "UDP":
                    inbound_udp.add(mp.port)
                inbound_by_sp[(mp.targetServerName, proto)].add(mp.port)

    # Explode into per-port entries with index
    mapped_by_protocol = []
    idx = 0
    for (server, proto), ports in sorted(outbound_by_sp.items()):
        for port in sorted(ports):
            mapped_by_protocol.append(AppImportPortByProtocol(
                index=idx, serverName=server, service="",
                protocol=proto, port=port,
            ))
            idx += 1

    mapped_by_protocol_inbound = []
    idx = 0
    for (server, proto), ports in sorted(inbound_by_sp.items()):
        for port in sorted(ports):
            mapped_by_protocol_inbound.append(AppImportPortByProtocol(
                index=idx, serverName=server, service="",
                protocol=proto, port=port,
            ))
            idx += 1

    peer_servers = set(mp.targetServerName for mp in mapped_ports if mp.targetServerName != server_name)
    inbound_count = sum(1 for d in port_directions if d == "inbound")

    return AppImportServer(
        id=server_id,
        sourceServer=server_name,
        totalMappedPorts=len(mapped_ports),
        totalMappedInboundPorts=inbound_count,
        totalMappedServers=len(peer_servers),
        mappedPorts=mapped_ports,
        allInboundPortsTcp=sorted(inbound_tcp),
        allOutboundPortsTcp=sorted(outbound_tcp),
        allInboundPortsUdp=sorted(inbound_udp),
        allOutboundPortsUdp=sorted(outbound_udp),
        mappedPortsByProtocol=mapped_by_protocol,
        mappedPortsByProtocolInbound=mapped_by_protocol_inbound,
    )


@app.get("/search", response_model=List[PortResponse])
async def search_ports(q: str):
    pattern = f"%{q}%"
    stmt = select(all_ports).where(
        or_(
            all_ports.c.sourceService.like(pattern),
            all_ports.c.targetService.like(pattern),
            all_ports.c.description.like(pattern),
            all_ports.c.port.like(pattern),
            all_ports.c.subheading.like(pattern),
            all_ports.c.product.like(pattern),
        )
    )
    df = pd.read_sql(stmt, engine)
    records = df.to_dict("records")
    return [PortResponse(**record) for record in records]


@app.get("/search/port/{port}", response_model=List[PortResponse])
async def search_by_port(port: str):
    stmt = select(all_ports).where(all_ports.c.port.like(f"%{port}%"))
    df = pd.read_sql(stmt, engine)
    records = df.to_dict("records")
    return [PortResponse(**record) for record in records]


@app.post("/semantic-search", response_model=SemanticSearchResponse)
async def semantic_search(request: SemanticSearchRequest):
    """Semantic search over enriched port data using vector embeddings."""
    if not _vectors_available:
        return _keyword_fallback(request)

    try:
        from embedder import embed_query

        query_bytes = embed_query(request.query)

        # Over-fetch when no product filter to compensate for cross-product dedup
        fetch_limit = request.limit if request.product else request.limit * 3

        with engine.connect() as conn:
            if request.product:
                vec_rows = conn.execute(
                    text("""
                        SELECT enriched_rowid, distance
                        FROM port_embeddings
                        WHERE embedding MATCH :q AND k = :limit AND product = :product
                    """),
                    {"q": query_bytes, "limit": fetch_limit, "product": request.product},
                ).fetchall()
            else:
                vec_rows = conn.execute(
                    text("""
                        SELECT enriched_rowid, distance
                        FROM port_embeddings
                        WHERE embedding MATCH :q AND k = :limit
                    """),
                    {"q": query_bytes, "limit": fetch_limit},
                ).fetchall()

            if not vec_rows:
                return SemanticSearchResponse(results=[], query=request.query)

            rowid_distance = {row[0]: row[1] for row in vec_rows}
            rowid_list = list(rowid_distance.keys())

            placeholders = ",".join(f":r{i}" for i in range(len(rowid_list)))
            params = {f"r{i}": rid for i, rid in enumerate(rowid_list)}
            enriched_rows = conn.execute(
                text(f"""
                    SELECT rowid, * FROM enriched_ports
                    WHERE rowid IN ({placeholders})
                """),
                params,
            ).mappings().all()

        results = []
        for row in enriched_rows:
            distance = rowid_distance.get(row["rowid"], 1.0)
            similarity = 1.0 - distance

            source_meta, target_meta = _build_service_metas(row)

            results.append(SemanticSearchResult(
                subheading=row["subheading"],
                subheadingL2=row["subheadingL2"],
                subheadingL3=row["subheadingL3"],
                product=row["product"],
                sourceService=row["sourceService"],
                targetService=row["targetService"],
                protocol=row["protocol"],
                port=row["port"],
                description=row["description"],
                source_meta=source_meta,
                target_meta=target_meta,
                similarity=round(similarity, 4),
            ))

        results.sort(key=lambda r: r.similarity, reverse=True)

        # Deduplicate cross-product results when no product filter is specified.
        # Same port rule appears in multiple products (e.g. VBR v13, VBR VMware v12.3);
        # keep only the highest-similarity match for each unique port rule.
        if not request.product:
            seen = set()
            deduped = []
            for r in results:
                key = (
                    r.source_meta.canonical,
                    r.target_meta.canonical,
                    r.port,
                    r.protocol,
                )
                if key not in seen:
                    seen.add(key)
                    deduped.append(r)
            results = deduped

        return SemanticSearchResponse(
            results=results[:request.limit], query=request.query
        )

    except (OperationalError, ValueError):
        log.exception("Vector search failed, falling back to keyword search")
        return _keyword_fallback(request)


def _keyword_fallback(request: SemanticSearchRequest) -> SemanticSearchResponse:
    """Fall back to keyword search on enriched_ports when vectors are unavailable."""
    pattern = f"%{request.query}%"

    # Over-fetch when no product filter to compensate for cross-product dedup
    fetch_limit = request.limit if request.product else request.limit * 3

    try:
        with engine.connect() as conn:
            query = text("""
                SELECT * FROM enriched_ports
                WHERE (sourceService LIKE :q OR targetService LIKE :q
                       OR description LIKE :q OR port LIKE :q
                       OR source_canonical LIKE :q OR target_canonical LIKE :q)
                AND (:product IS NULL OR product = :product)
                LIMIT :limit
            """)
            rows = conn.execute(
                query, {"q": pattern, "product": request.product, "limit": fetch_limit}
            ).mappings().all()
    except OperationalError:
        return SemanticSearchResponse(results=[], query=request.query, fallback=True)

    results = []
    for row in rows:
        source_meta, target_meta = _build_service_metas(row)
        results.append(SemanticSearchResult(
            subheading=row["subheading"],
            subheadingL2=row["subheadingL2"],
            subheadingL3=row["subheadingL3"],
            product=row["product"],
            sourceService=row["sourceService"],
            targetService=row["targetService"],
            protocol=row["protocol"],
            port=row["port"],
            description=row["description"],
            source_meta=source_meta,
            target_meta=target_meta,
            similarity=0.0,
        ))

    # Deduplicate cross-product results when no product filter
    if not request.product:
        seen = set()
        deduped = []
        for r in results:
            key = (
                r.source_meta.canonical,
                r.target_meta.canonical,
                r.port,
                r.protocol,
            )
            if key not in seen:
                seen.add(key)
                deduped.append(r)
        results = deduped

    return SemanticSearchResponse(
        results=results[:request.limit], query=request.query, fallback=True
    )


@app.post("/generateExcelWithUrl")
async def generate_excel_with_url(data: List[dict]):
    df = pd.DataFrame(data)
    clean_up_old_files("./reports")
    unique_filename = f"{uuid.uuid4()}.xlsx"
    file_path = os.path.join("./reports", unique_filename)
    df.to_excel(file_path, index=False)
    file_url = f"/download/{unique_filename}"
    return {"file_url": file_url}


@app.get("/download/{filename}")
async def download_file(filename: str):
    file_path = os.path.join("./reports", filename)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="File not found")
    response = FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )

    return response


os.makedirs("./reports", exist_ok=True)
