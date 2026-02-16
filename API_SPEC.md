# Veeam Ports Server — API Specification

Base URL: `https://magicports.veeambp.com/ports_server` (production) or `http://localhost:8001` (local dev)

All endpoints return JSON. POST endpoints accept `Content-Type: application/json`.

---

## Endpoint Reference

### GET `/`
List all available product names.

**Response:** `string[]`

```json
["VBR v13", "VBR v12", "VONE v13"]
```

---

### GET `/health`
Health check.

**Response:**
```json
{"status": "healthy"}
```

---

### GET `/products/{name}/ports`
Get all port entries for a product.

**Path params:** `name` — product name (URL-encoded, e.g. `VBR%20v13`)

**Response:** `PortResponse[]`

```json
[
  {
    "subheading": "Backup Infrastructure",
    "subheadingL2": "Backup Server",
    "subheadingL3": "",
    "product": "VBR v13",
    "sourceService": "Backup Server",
    "targetService": "ESXi Host",
    "protocol": "TCP",
    "port": "443",
    "description": "Required for VMware vSphere API communication"
  }
]
```

---

### GET `/products/{name}/subheadings`
Get distinct subheadings (section names) for a product.

**Path params:** `name` — product name

**Response:** `string[]`

```json
["Backup Infrastructure", "Cloud Connect", "Tape Infrastructure"]
```

---

### GET `/products/{name}/enriched-ports`
Get enriched port data with LLM-parsed service metadata. Returns 404 if enrichment hasn't been run.

**Path params:** `name` — product name

**Response:** `EnrichedPortResponse[]`

```json
[
  {
    "subheading": "Backup Infrastructure",
    "subheadingL2": "Backup Server",
    "subheadingL3": "",
    "product": "VBR v13",
    "sourceService": "Backup Server",
    "targetService": "ESXi Host",
    "protocol": "TCP",
    "port": "443",
    "description": "Required for VMware vSphere API communication",
    "source_meta": {
      "canonical": "backup server",
      "roles": ["backup", "management"],
      "os": "windows",
      "hypervisor": null,
      "storage_type": null,
      "original": "Backup Server"
    },
    "target_meta": {
      "canonical": "esxi host",
      "roles": ["hypervisor"],
      "os": null,
      "hypervisor": "vmware",
      "storage_type": null,
      "original": "ESXi Host"
    }
  }
]
```

---

### POST `/products/{name}/topology`
Resolve server topology — given named servers with assigned services, returns all port mappings between them via knowledge graph traversal.

**Path params:** `name` — product name

**Request body:**
```json
{
  "servers": [
    {"name": "VBR-01", "services": ["Backup Server", "Console"]},
    {"name": "ESXI-01", "services": ["ESXi Host"]},
    {"name": "PROXY-01", "services": ["VMware Backup Proxy"]}
  ],
  "options": {
    "include_loopback": false,
    "include_unresolved": false
  }
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `servers` | `TopologyServer[]` | yes | — | List of servers with their services |
| `servers[].name` | `string` | yes | — | User-defined server name |
| `servers[].services` | `string[]` | yes | — | Service names running on this server (use names from `sourceService`/`targetService` fields) |
| `options.include_loopback` | `bool` | no | `false` | Include port mappings where source and target resolve to the same server |
| `options.include_unresolved` | `bool` | no | `false` | Include unresolved service names in metadata |

**Response:** `TopologyResponse`

```json
{
  "product": "VBR v13",
  "servers": [
    {
      "id": "uuid-here",
      "sourceServer": "VBR-01",
      "totalMappedPorts": 12,
      "totalMappedInboundPorts": 3,
      "totalMappedServers": 2,
      "mappedPorts": [
        {
          "server": "ESXI-01",
          "port": "443",
          "protocol": "TCP",
          "description": "Required for VMware vSphere API communication",
          "direction": "outbound"
        }
      ],
      "allInboundPortsTcp": ["9392", "9393"],
      "allOutboundPortsTcp": ["443", "902"],
      "allInboundPortsUdp": [],
      "allOutboundPortsUdp": [],
      "mappedPortsByProtocol": [
        {"server": "ESXI-01", "protocol": "TCP", "ports": ["443", "902"]}
      ],
      "mappedPortsByProtocolInbound": [
        {"server": "PROXY-01", "protocol": "TCP", "ports": ["9392"]}
      ]
    }
  ],
  "metadata": {
    "total_entries_matched": 45,
    "total_entries_skipped": 120,
    "enrichment_version": "2025-01-15T12:00:00Z",
    "unresolved_services": []
  }
}
```

---

### POST `/products/{name}/app-import`
Generate port mapping entries for Magic Ports frontend import. Same request body as `/topology`, returns a server-centric nested structure matching the frontend import schema. Each server's `mappedPorts[]` contains **outbound-only** entries (where the server is the connection initiator). The frontend derives inbound port requirements by cross-referencing all servers' entries where `targetServerName` matches.

**Path params:** `name` — product name

**Request body:** Same as `/products/{name}/topology` (see above)

**Response:** `AppImportServer[]`

```json
[
  {
    "id": "uuid-here",
    "sourceServer": "VBR Appliance",
    "totalMappedPorts": 9,
    "totalMappedInboundPorts": 0,
    "totalMappedServers": 4,
    "mappedPorts": [
      {
        "sourceServerId": "uuid-here",
        "sourceServerName": "VBR Appliance",
        "targetServerName": "Linux Proxy",
        "sourceService": "Backup server",
        "targetService": "Backup proxy (Linux)",
        "description": "Default SSH port used as a control channel.",
        "product": "VBR v13",
        "port": "22",
        "protocol": "TCP"
      }
    ],
    "allInboundPortsTcp": [],
    "allOutboundPortsTcp": ["22", "443", "6160", "6162, 2500 to 3300"],
    "allInboundPortsUdp": [],
    "allOutboundPortsUdp": [],
    "mappedPortsByProtocol": [
      {"index": 0, "serverName": "Linux Proxy", "service": "", "protocol": "TCP", "port": "22"},
      {"index": 1, "serverName": "Linux Proxy", "service": "", "protocol": "TCP", "port": "6160"}
    ],
    "mappedPortsByProtocolInbound": []
  }
]
```

**Root fields** (same as `/topology`):

| Field | Type | Description |
|-------|------|-------------|
| `id` | `string` | Unique UUID for this server entry |
| `sourceServer` | `string` | User-defined server name (from request) |
| `totalMappedPorts` | `int` | Total outbound port mappings |
| `totalMappedInboundPorts` | `int` | Always 0 (frontend computes inbound via cross-reference) |
| `totalMappedServers` | `int` | Number of distinct peer servers |
| `allInboundPortsTcp` / `allInboundPortsUdp` | `string[]` | Always `[]` (frontend computes inbound) |
| `allOutboundPortsTcp` / `allOutboundPortsUdp` | `string[]` | Deduplicated outbound port lists |

**`mappedPorts[]`** entries:

| Field | Type | Description |
|-------|------|-------------|
| `sourceServerId` | `string` | UUID of the parent server entry |
| `sourceServerName` | `string` | Name of the parent server |
| `targetServerName` | `string` | Peer server name |
| `sourceService` | `string` | Source service name from port data |
| `targetService` | `string` | Target service name from port data |
| `description` | `string` | Port description from Veeam docs |
| `product` | `string` | Product name |
| `port` | `string` | Port number or range |
| `protocol` | `string` | "TCP", "UDP", "TCP, UDP", etc. |

**`mappedPortsByProtocol[]`** entries (outbound; `mappedPortsByProtocolInbound` is always `[]`):

| Field | Type | Description |
|-------|------|-------------|
| `index` | `int` | 0-based position in the array |
| `serverName` | `string` | Peer server name |
| `service` | `string` | Reserved (currently `""`) |
| `protocol` | `string` | "TCP" or "UDP" |
| `port` | `string` | Single port number or range |

---

### POST `/semantic-search`
Semantic (vector) search over enriched port data. Uses cosine similarity on 384-dim embeddings. Falls back to keyword search if vectors are unavailable.

**Request body:**
```json
{
  "query": "what ports does the proxy need for VMware",
  "product": "VBR v13",
  "limit": 10
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `query` | `string` | yes | — | Natural language search query |
| `product` | `string` | no | `null` | Filter results to a specific product. If null, searches across all products |
| `limit` | `int` | no | `20` | Max results (1-100) |

**Response:** `SemanticSearchResponse`

```json
{
  "results": [
    {
      "subheading": "Backup Infrastructure",
      "subheadingL2": "VMware Backup Proxy",
      "subheadingL3": "",
      "product": "VBR v13",
      "sourceService": "VMware Backup Proxy",
      "targetService": "ESXi Host",
      "protocol": "TCP",
      "port": "902",
      "description": "Used for NBD data transport from ESXi",
      "source_meta": {
        "canonical": "vmware backup proxy",
        "roles": ["proxy", "backup"],
        "os": "windows",
        "hypervisor": "vmware",
        "storage_type": null,
        "original": "VMware Backup Proxy"
      },
      "target_meta": {
        "canonical": "esxi host",
        "roles": ["hypervisor"],
        "os": null,
        "hypervisor": "vmware",
        "storage_type": null,
        "original": "ESXi Host"
      },
      "similarity": 0.7312
    }
  ],
  "query": "what ports does the proxy need for VMware",
  "fallback": false
}
```

| Response field | Type | Description |
|----------------|------|-------------|
| `results` | `SemanticSearchResult[]` | Matched port entries, sorted by similarity descending |
| `results[].similarity` | `float` | Cosine similarity score (0.0 - 1.0). Higher = more relevant. When `fallback=true`, always 0.0 |
| `query` | `string` | Echo of the input query |
| `fallback` | `bool` | `true` if keyword search was used instead of vector search (vectors unavailable or search failed) |

**Example queries that work well:**
- `"what ports does the proxy need for VMware"` — matches proxy/ESXi entries even though "VMware" isn't in service names
- `"firewall rules for backup to NAS"` — matches NFS/SMB repository entries
- `"cloud connectivity"` — matches cloud gateway, object storage entries
- `"database ports"` — matches SQL Server, PostgreSQL entries

---

### GET `/search?q={query}`
Keyword (LIKE) search across all fields in the raw `all_ports` table.

**Query params:** `q` — search string

**Response:** `PortResponse[]`

---

### GET `/search/port/{port}`
Find entries by port number (LIKE match).

**Path params:** `port` — port number or substring

**Response:** `PortResponse[]`

---

### POST `/source`
Get distinct source service names for a product.

**Request body:**
```json
{"productName": "VBR v13"}
```

**Response:** `string[]`

---

### POST `/sourceDetails`
Get source services grouped with their subheadings.

**Request body:**
```json
{"productName": "VBR v13"}
```

**Response:** `SourceResponse[]`

```json
[
  {"product": "VBR v13", "subheading": "Backup Infrastructure", "sourceService": "Backup Server"}
]
```

---

### POST `/target`
Get distinct target service names for a given source service and product.

**Request body:**
```json
{
  "productName": "VBR v13",
  "sourceService": "Backup Server",
  "subheading": "Backup Infrastructure"
}
```

**Response:** `string[]`

---

### POST `/allTarget`
Get all port entries for a source service under a specific subheading.

**Request body:**
```json
{
  "productName": "VBR v13",
  "sourceService": "Backup Server",
  "subheading": "Backup Infrastructure"
}
```

**Response:** `PortResponse[]`

---

### POST `/ports`
Get port entries for a specific source-target-product combination.

**Request body:**
```json
{
  "productName": "VBR v13",
  "sourceService": "Backup Server",
  "targetService": "ESXi Host"
}
```

**Response:** `PortResponse[]`

---

## Shared Types

### `PortResponse`
```typescript
{
  subheading: string
  subheadingL2: string
  subheadingL3: string
  product: string
  sourceService: string
  targetService: string
  protocol: string      // "TCP", "UDP", "TCP, UDP", etc.
  port: string          // "443", "6160-6170", "Dynamic", etc.
  description: string
}
```

### `ServiceMeta`
LLM-parsed metadata for a service name. Present in enriched endpoints.
```typescript
{
  canonical: string        // Normalized lowercase name (e.g. "esxi host")
  roles: string[]          // Functional roles (e.g. ["hypervisor", "compute"])
  os: string | null        // "windows", "linux", or null
  hypervisor: string | null  // "vmware", "hyperv", or null
  storage_type: string | null  // "nas", "san", "dedup", "object", or null
  original: string         // Original service name as it appears in the data
}
```

---

## MCP Tool Design Recommendations

The following endpoints are most useful as MCP tools for an LLM agent:

| Tool | Endpoint | When to use |
|------|----------|-------------|
| `list_products` | `GET /` | First step — discover available products |
| `semantic_search` | `POST /semantic-search` | Best for natural language questions about ports. Start here for most queries |
| `get_enriched_ports` | `GET /products/{name}/enriched-ports` | Get full enriched port list for a product (includes service metadata) |
| `generate_topology` | `POST /products/{name}/topology` | When the user describes their server layout and wants firewall rules (nested format) |
| `generate_app_import` | `POST /products/{name}/app-import` | When the user wants to generate a flat import file for the Magic Ports frontend |
| `get_product_ports` | `GET /products/{name}/ports` | Get raw port data for a product (no enrichment needed) |
| `search_ports` | `GET /search?q=` | Simple keyword search as a fallback |

**Recommended workflow for an MCP agent:**
1. Call `list_products` to discover available products
2. Use `semantic_search` for natural language questions — it handles synonyms and related concepts
3. If the user describes servers/services, use `generate_topology` with their server layout
4. Fall back to `get_enriched_ports` or `get_product_ports` for exhaustive data
