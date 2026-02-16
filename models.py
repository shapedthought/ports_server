from pydantic import BaseModel, Field


class PortResponse(BaseModel):
    description: str = Field(..., alias="description")
    product: str = Field(..., alias="product")
    sourceService: str = Field(..., alias="sourceService")
    targetService: str = Field(..., alias="targetService")
    subheading: str = Field(..., alias="subheading")
    subheadingL2: str = Field(..., alias="subheadingL2")
    subheadingL3: str = Field(..., alias="subheadingL3")
    port: str = Field(..., alias="port")
    protocol: str = Field(..., alias="protocol")


class TargetRequest(BaseModel):
    sourceService: str
    productName: str
    subheading: str


class PortRequest(BaseModel):
    productName: str
    sourceService: str
    targetService: str


class SourceRequest(BaseModel):
    productName: str


class SourceResponse(BaseModel):
    product: str
    subheading: str
    sourceService: str


class ServiceMeta(BaseModel):
    canonical: str
    roles: list[str]
    os: str | None = None
    hypervisor: str | None = None
    storage_type: str | None = None
    original: str


class EnrichedPortResponse(BaseModel):
    subheading: str = Field(..., alias="subheading")
    subheadingL2: str = Field(..., alias="subheadingL2")
    subheadingL3: str = Field(..., alias="subheadingL3")
    product: str = Field(..., alias="product")
    sourceService: str = Field(..., alias="sourceService")
    targetService: str = Field(..., alias="targetService")
    protocol: str = Field(..., alias="protocol")
    port: str = Field(..., alias="port")
    description: str = Field(..., alias="description")
    source_meta: ServiceMeta
    target_meta: ServiceMeta


# --- Topology request/response models (Phase 2) ---


class TopologyServer(BaseModel):
    name: str
    services: list[str]


class TopologyOptions(BaseModel):
    include_loopback: bool = False
    include_unresolved: bool = False


class TopologyRequest(BaseModel):
    servers: list[TopologyServer]
    options: TopologyOptions = TopologyOptions()


class MappedPort(BaseModel):
    server: str
    port: str
    protocol: str
    description: str
    direction: str


class MappedPortByProtocol(BaseModel):
    server: str
    protocol: str
    ports: list[str]


class ServerTopology(BaseModel):
    id: str
    sourceServer: str
    totalMappedPorts: int
    totalMappedInboundPorts: int
    totalMappedServers: int
    mappedPorts: list[MappedPort]
    allInboundPortsTcp: list[str]
    allOutboundPortsTcp: list[str]
    allInboundPortsUdp: list[str]
    allOutboundPortsUdp: list[str]
    mappedPortsByProtocol: list[MappedPortByProtocol]
    mappedPortsByProtocolInbound: list[MappedPortByProtocol]


class TopologyMetadata(BaseModel):
    total_entries_matched: int
    total_entries_skipped: int
    enrichment_version: str | None = None
    unresolved_services: list[str] = []


class TopologyResponse(BaseModel):
    product: str
    servers: list[ServerTopology]
    metadata: TopologyMetadata
