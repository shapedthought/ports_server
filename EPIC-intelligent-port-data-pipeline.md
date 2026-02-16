# Epic: Intelligent Port Data Pipeline

## Overview

Replace the fragile runtime matching logic in the Veeam Ports MCP server with an LLM-enriched data pipeline that normalises service names at scrape time, a knowledge graph for deterministic topology generation, and optional vector search for exploratory queries.

### Problem Statement

The MCP server's `_find_server` function currently performs fuzzy matching at query time to resolve raw service names (e.g. `"Hyper-V server/Off-host backup proxy"`) against user-defined servers. This has required an escalating series of patches — OS conflict detection, storage type detection, hypervisor detection — and a proposed refactor into a tag-based parser. Each new qualifier dimension in the scraped data risks breaking the matching logic.

The root cause is that the scraped data is flat and inconsistent. All normalisation intelligence lives in the matching code, which runs on every request. Moving this intelligence to scrape time via LLM enrichment eliminates the problem at source.

### Success Criteria

- `_find_server` reduces to simple canonical name + tag equality (no substring matching, no conflict detection chain)
- `generate_app_import` produces correct topology for any combination of OS, hypervisor, and storage type without runtime heuristics
- New qualifier dimensions (e.g. new Veeam product versions with new service types) require zero code changes — only a re-scrape
- Exploratory queries like `search_ports` return semantically relevant results even when terminology doesn't match exactly

### Architecture

```
BACKEND SERVICE (k3s)
═══════════════════════════════════════════════════════════════

Phase 1: LLM Enrichment at Scrape Time
┌─────────┐     ┌───────────┐     ┌──────────────┐     ┌─────────┐
│ Scraper  │────▶│ Raw Entry │────▶│ LLM Enricher │────▶│ API/DB  │
└─────────┘     └───────────┘     │ (your key,   │     └─────────┘
                                  │  Haiku)       │
                                  └──────────────┘

Phase 2: Knowledge Graph
┌─────────────┐     ┌───────────────┐     ┌─────────────────┐
│ Enriched DB │────▶│ Graph Builder │────▶│ SQLite adjacency│
└─────────────┘     └───────────────┘     └─────────────────┘
                                                │
                                                ▼
                                    POST /topology endpoint
                                    (resolves server topology)

Phase 3: Vector Search (optional)
┌─────────────┐     ┌────────────┐     ┌──────────────────┐
│ Enriched DB │────▶│ Embeddings │────▶│ pgvector / Chroma│
└─────────────┘     └────────────┘     └──────────────────┘
                                                │
                                                ▼
                                    POST /semantic-search endpoint

Phase 4: Chat with BYOK
┌──────────────┐     ┌─────────────┐     ┌──────────────┐
│ POST /chat   │────▶│ RAG context │────▶│ LLM Provider │
│ (user's msg) │     │ retrieval   │     │ (user's key) │
└──────────────┘     └─────────────┘     └──────────────┘

CONSUMERS
═══════════════════════════════════════════════════════════════

MCP Server (thin client)          Frontend App (Angular)
┌─────────────────────┐           ┌──────────────────────┐
│ generate_app_import │           │ Manual port mapping  │
│  → POST /topology   │           │  → existing endpoints│
│  → write file       │           │ Chat panel (Phase 4) │
│  → return summary   │           │  → POST /chat        │
│                     │           │ Topology builder      │
│ search_ports        │           │  → POST /topology     │
│  → GET /search      │           │  (optional, Phase 2+)│
└─────────────────────┘           └──────────────────────┘
```

---

## Phase 1: LLM Enrichment at Scrape Time

### Objective

Enrich each scraped port entry with structured metadata (canonical name, roles, OS, hypervisor, storage type) using an LLM at ingest time, eliminating the need for runtime fuzzy matching.

### k3s Deployment Changes (Phase 1)

**API key secret.** The CronJob needs an Anthropic API key for Haiku enrichment calls. Create a `Secret` and wire it into the CronJob:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: veeam-ports-enrichment
  namespace: veeam-ports
type: Opaque
stringData:
  ANTHROPIC_API_KEY: "sk-ant-..."
```

Add to the CronJob spec:

```yaml
envFrom:
  - secretRef:
      name: veeam-ports-enrichment
```

**PVC sizing.** The current 100Mi PVC is tight once enriched columns and (Phase 2) graph tables are added. Bump to 500Mi proactively. The data itself is small, but SQLite WAL files and temp sort space during rebuilds need headroom.

**SQLite WAL mode.** Enable WAL mode on the database to reduce write lock contention. The scraper and backend don't currently run simultaneously (`concurrencyPolicy: Forbid`), but WAL is cheap insurance and a prerequisite for Phase 2 where the graph builder writes more data:

```python
# On connection open
conn.execute("PRAGMA journal_mode=WAL")
```

### Migration Strategy

The scraper currently drops and recreates `all_ports` each run. This is fine for raw data but dangerous for enriched data, which costs LLM calls to reproduce. If a scrape fails mid-enrichment, you lose both raw and enriched data.

**Approach: file-level atomic swap from day one.**

Rather than staging tables within the production database, build the entire database to a temp file and swap it into place on success. This is the same pattern Phase 2 uses for graph tables, so adopting it now means one consistent approach across all phases — no table-rename code to write and then delete.

1. Scraper creates a fresh `ports_staging.db` file
2. Writes raw data to `all_ports` table in the staging file
3. Enricher processes entries, writes to `enriched_ports` table in the staging file
4. Validates enrichment (schema checks, entry count sanity)
5. On full success: `os.replace("ports_staging.db", "ports.db")` — atomic on same filesystem
6. On failure: staging file is abandoned or deleted, production database untouched

```python
import os

STAGING_DB = "/data/ports_staging.db"
PRODUCTION_DB = "/data/ports.db"

def build_and_swap():
    """Build complete database in staging, swap on success."""
    # Remove any leftover staging file from a previous failed run
    if os.path.exists(STAGING_DB):
        os.remove(STAGING_DB)

    conn = sqlite3.connect(STAGING_DB)
    conn.execute("PRAGMA journal_mode=WAL")

    try:
        scrape_all_ports(conn)          # Phase 1: raw data
        enrich_all_ports(conn)          # Phase 1: LLM enrichment
        # build_graph(conn)             # Phase 2: add when ready
        # embed_entries(conn)           # Phase 3: add when ready
        validate(conn)
        conn.close()
        os.replace(STAGING_DB, PRODUCTION_DB)  # Atomic swap
    except Exception:
        conn.close()
        os.remove(STAGING_DB)
        raise
```

The backend reopens the database on next request. This pattern extends cleanly: Phase 2 adds `build_graph(conn)` to the pipeline, Phase 3 adds `embed_entries(conn)`. No code changes to the swap logic itself.

---

### Issue 1.1: Define the enriched schema

**Type:** Design

**Description:**
Define the enriched port entry schema that the LLM will produce. This schema replaces the implicit knowledge currently embedded in `_normalise_service`, `_has_conflicting_os`, `_has_conflicting_type`, and the proposed `_has_conflicting_hypervisor` / `_parse_service` / `_tags_compatible` functions.

**Deliverable:** Schema definition (JSON Schema or Pydantic model) for enriched port entries.

**Proposed schema:**

```python
class ServiceMeta(BaseModel):
    """Structured metadata extracted from a raw service name."""
    canonical: str          # Normalised base name, e.g. "backup proxy"
    roles: list[str]        # All roles this service fulfils, e.g. ["backup proxy", "gateway server"]
    os: str | None          # "linux", "windows", or None (generic)
    hypervisor: str | None  # "vmware", "hyperv", "nutanix", or None (generic)
    storage_type: str | None  # "nfs", "smb", "object_storage", "hardened", or None (generic)
    original: str           # Original raw service name, preserved for reference

class EnrichedPortEntry(BaseModel):
    """A port entry with LLM-enriched source and target metadata."""
    source_service: str           # Raw source service name
    target_service: str           # Raw target service name
    port: str
    protocol: str
    description: str
    subheading: str | None
    subheading_l2: str | None
    product: str
    source_meta: ServiceMeta      # LLM-enriched metadata for source
    target_meta: ServiceMeta      # LLM-enriched metadata for target
```

**Acceptance criteria:**
- Schema handles all known qualifier dimensions: OS (linux, windows), hypervisor (vmware, hyperv, nutanix), storage type (nfs, smb, object_storage, hardened)
- Schema preserves the original raw names for debugging and backward compatibility
- Schema supports multi-role services (e.g. "Backup proxy or gateway server" → two roles)
- Schema is extensible — adding a new dimension is adding an optional field

---

### Issue 1.2: Build the LLM enrichment prompt and pipeline

**Type:** Implementation

**Depends on:** 1.1

**Description:**
Create a Python module that takes a raw scraped port entry and returns an enriched entry using an LLM call. The LLM extracts structured tags from the raw service names.

**Deliverable:** `enricher.py` module with an `enrich_entry()` function and a batch `enrich_all()` function.

**Key design decisions:**

- **Model:** Claude Haiku (cheapest, fast, more than capable for this structured extraction task). ~300 entries per product × ~500 input tokens per entry = negligible cost.
- **Approach:** Send batch of entries with a system prompt defining the schema and rules, ask for JSON output. Process in batches of 20-30 entries per call to reduce round trips.
- **Idempotency:** Cache enrichment results keyed by (product, raw_source, raw_target). Only re-enrich when the raw data changes.
- **Validation:** Validate LLM output against the Pydantic schema. Retry on validation failure (max 2 retries). Log and flag entries that fail enrichment for manual review.

**LLM system prompt (draft):**

```
You are a data normalisation assistant for Veeam Backup & Replication network port data.

For each port entry, extract structured metadata from the source and target service names.

Rules:
- canonical: The base service name with all qualifiers removed, lowercased. E.g. "Backup proxy (Linux)" → "backup proxy". "NFS backup repository" → "backup repository". "Hyper-V server/Off-host backup proxy" → the PRIMARY role, which is "backup proxy" in this case.
- roles: ALL roles this service can fulfil. Split on "or", "/", and similar conjunctions. E.g. "Backup proxy or gateway server" → ["backup proxy", "gateway server"]. Most entries have a single role.
- os: Extract if present. Normalise: "Linux", "Linux/Unix" → "linux". "Microsoft Windows", "Windows" → "windows". Null if not specified.
- hypervisor: Extract if present. "ESXi", "vCenter" → "vmware". "Hyper-V" → "hyperv". "Nutanix AHV" → "nutanix". Null if not specified.
- storage_type: Extract if present. "NFS" → "nfs". "SMB" → "smb". "Object storage" → "object_storage". "Hardened" → "hardened". Null if not specified.
- original: The exact original service name, unchanged.

Respond with ONLY a JSON array, no markdown, no preamble.
```

**Acceptance criteria:**
- Correctly enriches all VBR v13 entries (primary test product)
- Handles all known edge cases: compound names with "or"/"/" conjunctions, inconsistent OS naming ("Windows" vs "Microsoft Windows"), hypervisor-prefixed names, storage type prefixes
- Validated against Pydantic schema with zero failures on VBR v13 data
- Batch processing with configurable batch size
- Caching layer to avoid re-enriching unchanged entries
- Comprehensive error handling and logging

---

### Issue 1.3: Integrate enrichment into the scraping pipeline

**Type:** Implementation

**Depends on:** 1.2

**Description:**
Wire the enrichment module into the existing scraping process so that every scraped entry is automatically enriched before being stored. The enriched metadata should be stored alongside the raw data in the existing API/database.

**Deliverable:** Updated scraper that produces enriched entries. Updated API schema to serve enriched data.

**Key considerations:**
- Enrichment runs after scraping, before storage
- Store both raw and enriched data (raw for audit, enriched for queries)
- Add an API endpoint or extend the existing `/products/{name}/ports` response to include enriched metadata
- Consider a separate `/products/{name}/enriched-ports` endpoint if backward compatibility is a concern
- Add a `--re-enrich` flag to the scraper CLI for forcing re-enrichment of all entries

**Acceptance criteria:**
- Scraping pipeline produces enriched entries end-to-end
- API serves enriched metadata in port entry responses
- Backward compatibility maintained — existing API consumers still work
- Re-enrichment can be triggered manually

---

### ~~Issue 1.4: Simplify `_find_server` to use enriched data~~ — SKIPPED

**Status:** SKIP — go directly from 1.3 to Phase 2.

**Rationale:**
This was an interim MCP repo change to simplify the matching code using enriched data from the existing API endpoint. But since Phase 2 deletes all matching code from the MCP entirely (replaced by `POST /topology`), this is throwaway work. The enriched data can prove itself through the `/enriched-ports` endpoint and integration tests without touching the MCP server.

The existing `_find_server` with its `_has_conflicting_*` chain continues to work in the MCP until Issue 2.3 replaces it with a single API call. It's ugly but functional — no point polishing code that's about to be deleted.

---

## Phase 2: Knowledge Graph for Topology Generation

### Objective

Model the Veeam port data as a graph of components and connections, enabling `generate_app_import` to produce topologies by traversing edges rather than matching entries. Also unlocks new capabilities: version diffs, impact analysis, and shortest-path queries.

### k3s Deployment Changes (Phase 2)

**No new infrastructure.** The file-level atomic swap established in Phase 1 extends naturally — just add `build_graph(conn)` to the pipeline before the swap. Graph tables (`components`, `connections`, `component_variants`) live in the same SQLite file alongside `all_ports` and `enriched_ports`. The atomic swap ensures all tables are consistent with each other.

**No PVC or concurrency changes needed.** Phase 1's `os.replace()` swap already eliminates concurrent access concerns. The graph builder adds more write time to the pipeline but doesn't change the deployment model.

---

### Issue 2.1: Design the graph schema

**Type:** Design

**Description:**
Define the node types, relationship types, and property schema for the port data knowledge graph.

**Proposed schema:**

```
NODE TYPES:
  Product        {name: "VBR v13"}
  Component      {canonical: "backup proxy", os: "linux", hypervisor: null, ...}
  Port           {number: "443", protocol: "TCP"}

RELATIONSHIPS:
  (Product)-[:HAS_COMPONENT]->(Component)
  (Component)-[:CONNECTS_TO {port: "443", protocol: "TCP", description: "..."}]->(Component)
  (Component)-[:IS_VARIANT_OF]->(Component)   # e.g. Backup proxy (Linux) → Backup proxy
  (Component)-[:IS_TYPE_OF]->(Component)       # e.g. NFS backup repository → Backup repository
```

**Deliverable:** Graph schema document with node/relationship definitions, example data, and query patterns.

**Acceptance criteria:**
- Schema represents all qualifier dimensions (OS, hypervisor, storage type) as node properties
- Variant/type relationships enable traversal from specific to generic components
- Connection relationships carry full port metadata (port, protocol, description, direction)
- Schema supports multi-product data with version differentiation

---

### Issue 2.2: Build the graph from enriched data

**Type:** Implementation

**Depends on:** 1.3, 2.1

**Description:**
Create a pipeline that reads enriched port entries and builds a knowledge graph. Start with SQLite + adjacency tables for simplicity (no external infrastructure dependency). Consider Neo4j Aura free tier if graph query complexity justifies it later.

**Deliverable:** `graph_builder.py` module that populates the graph from enriched port data.

**SQLite approach (recommended starting point):**

```sql
CREATE TABLE components (
    id INTEGER PRIMARY KEY,
    product TEXT NOT NULL,
    canonical TEXT NOT NULL,
    os TEXT,
    hypervisor TEXT,
    storage_type TEXT,
    original_name TEXT,
    UNIQUE(product, canonical, os, hypervisor, storage_type)
);

CREATE TABLE connections (
    id INTEGER PRIMARY KEY,
    product TEXT NOT NULL,
    source_component_id INTEGER REFERENCES components(id),
    target_component_id INTEGER REFERENCES components(id),
    port TEXT NOT NULL,
    protocol TEXT NOT NULL,
    description TEXT,
    UNIQUE(product, source_component_id, target_component_id, port, protocol)
);

CREATE TABLE component_variants (
    id INTEGER PRIMARY KEY,
    specific_id INTEGER REFERENCES components(id),
    generic_id INTEGER REFERENCES components(id)
);
```

**Acceptance criteria:**
- Graph correctly represents all VBR v13 port data
- Components are deduplicated (same canonical + tags = same node)
- Variant relationships link OS/hypervisor/type-specific components to their generic base
- Graph can be rebuilt from scratch in under 10 seconds for a single product
- Graph is queryable: "given components A, B, C, return all connections between them"

---

### Issue 2.3: Rewrite `generate_app_import` to use graph traversal

**Type:** Implementation

**Depends on:** 2.2

**Description:**
Replace the `_build_app_import` function with a graph query. Given the user's server definitions, resolve each to a graph node, then query all connections between the resolved nodes.

**Proposed approach:**

```python
async def generate_app_import(...):
    # 1. Resolve user servers to graph component IDs
    resolved = []
    for srv in servers:
        for svc in srv["services"]:
            component = graph.find_component(product, svc)  # Uses enriched matching
            resolved.append((srv["name"], component))

    # 2. Get all connections between resolved components
    component_ids = [c.id for _, c in resolved]
    connections = graph.get_connections_between(product, component_ids)

    # 3. Build the import JSON from the connection results
    # (Same output structure as current _build_app_import, but sourced from graph)
```

**Acceptance criteria:**
- Produces identical output to the current `_build_app_import` for the test configurations
- No fuzzy matching logic — all matching is done via graph node resolution
- Loopback connections excluded (source_component_id != target_component_id)
- Only connections between user-defined servers included (no unresolved targets)
- Performance: under 500ms for a 5-server VBR v13 query

---

### Issue 2.4: Add version diff and impact analysis queries (stretch)

**Type:** Feature

**Depends on:** 2.3

**Description:**
Leverage the graph to enable new MCP tools:
- `compare_versions(product_a, product_b)` — show added/removed/changed ports between versions
- `impact_analysis(product, component)` — show all components affected if a given component goes down
- `minimum_ports(product, components)` — the minimal set of unique ports needed for a given topology

These are new tools, not modifications to existing ones.

**Acceptance criteria:**
- At least one new tool implemented and registered with the MCP server
- Returns useful, accurate results for VBR v12 → v13 comparison (if v12 data is available)

---

## Phase 3: Vector Search for Exploratory Queries (Optional)

### Objective

Add semantic search over enriched port entries to improve the `search_ports` and `search_by_port_number` tools, enabling natural language queries that don't require exact keyword matches.

### k3s Deployment Changes (Phase 3)

**No new infrastructure.** Use `sqlite-vec` to keep vector search in the same SQLite file as everything else. This avoids deploying a separate vector database (Qdrant, pgvector, etc.) and keeps the "single file, atomic swap" deployment model from Phase 2 intact.

`sqlite-vec` is a loadable SQLite extension that adds vector similarity search. Embeddings are stored as a virtual table in the same `.db` file. The scraper generates embeddings at enrichment time and writes them to the staging database alongside the enriched data and graph tables. The atomic swap deploys everything together.

If `sqlite-vec` proves too limited (unlikely at ~300 entries per product), Chroma in-process is the fallback — it embeds into the FastAPI process and persists to the PVC. But avoid deploying a separate database for this.

**Embedding API key.** If using Voyage or OpenAI for embeddings, another secret is needed in the CronJob. Alternatively, use a local embedding model (e.g. `sentence-transformers` via `torch`) to avoid the API dependency entirely — the dataset is small enough that CPU inference is fine.

---

### Issue 3.1: Generate and store embeddings for enriched entries

**Type:** Implementation

**Depends on:** 1.3

**Description:**
Embed each enriched port entry (canonical name + tags + description) using an embedding model. Store in `sqlite-vec` within the same SQLite database.

**What to embed (per entry):**
```
{canonical source} → {canonical target} | Port: {port} {protocol} | {description} | OS: {os} | Hypervisor: {hypervisor} | Type: {storage_type}
```

**Key decisions:**
- Embedding model: Local `sentence-transformers` (zero cost, no API key) or Voyage/OpenAI text-embedding-3-small (cheap, slightly better quality). Start with local.
- Storage: `sqlite-vec` virtual table in the same database file. Falls through the atomic swap pipeline from Phase 2.
- Embed at scrape time alongside LLM enrichment
- Re-embed when enrichment data changes

**Acceptance criteria:**
- All enriched entries embedded and stored
- Embedding pipeline runs as part of the scrape process
- Vector store is queryable with cosine similarity

---

### Issue 3.2: Add semantic search tool to the MCP server

**Type:** Implementation

**Depends on:** 3.1

**Description:**
Add a `semantic_search_ports` tool (or enhance existing `search_ports`) that uses vector similarity to find relevant port entries. Falls back to keyword search if the vector store is unavailable.

**Example queries that should work:**
- "what ports does the proxy need for VMware" → matches ESXi/vCenter entries even though "VMware" doesn't appear in the data
- "firewall rules for backup to NAS" → matches NFS/SMB repository entries
- "cloud connectivity" → matches cloud gateway, object storage entries

**Acceptance criteria:**
- Returns semantically relevant results for natural language queries
- Results include similarity score for ranking
- Graceful fallback to keyword search if vector store is unavailable
- Response format matches existing `search_ports` output

---

## Repository Ownership

All data intelligence lives in the backend service. The MCP repo stays thin.

```
Backend Service (k3s)                    MCP Server
─────────────────────                    ──────────
Scraping job                             Thin API client
LLM enrichment pipeline                  generate_app_import → calls POST /topology
Graph builder + storage                  Writes file to disk, returns summary
Vector embeddings + storage              search_ports → calls /search or /semantic-search
Enriched API endpoints                   All other tools → thin wrappers around GET endpoints
Topology resolution endpoint
BYOK LLM key management
Chat/query endpoint (future)
```

### What goes where

**Backend repo changes:**
- Enrichment module (`enricher.py`) — runs in the scraping CronJob
- Database migrations — enriched columns, graph tables, vector extension
- New API endpoints — `/topology`, `/enriched-ports`, `/semantic-search`, `/chat` (future)
- BYOK key management — storage, validation, proxy to LLM providers

**MCP repo changes:**
- Phase 1: No changes needed (enriched data flows through existing API shape)
- Phase 1.4: Simplify `_find_server` (interim, uses enriched data from existing endpoint)
- Phase 2.3: Replace `_build_app_import` with a single `POST /topology` call, delete all matching code
- Phase 3: Optionally switch `search_ports` to call `/semantic-search` instead of `/search`

---

## Backend API Contract

These endpoints define the interface between the backend and all consumers (MCP, frontend, future chat). They must be designed before Phase 2 development begins so both repos can develop in parallel.

### Existing endpoints (no breaking changes)

All existing endpoints continue to work unchanged. Enriched data is additive.

```
GET  /                                → List products (unchanged)
GET  /products/{name}/ports           → Raw port entries (unchanged)
GET  /products/{name}/subheadings     → Section headings (unchanged)
GET  /search?q={query}                → Keyword search (unchanged)
GET  /search/port/{port}              → Port number search (unchanged)
POST /sourceDetails                   → Source services grouped by section (unchanged)
GET  /health                          → Health check (unchanged)
```

### New endpoints (additive)

```
GET  /products/{name}/enriched-ports  → Port entries with enriched metadata
POST /products/{name}/topology        → Resolve server topology (replaces client-side matching)
GET  /products/{name}/components      → List enriched components for a product
GET  /products/{name}/graph           → Full graph structure (components + connections)
POST /semantic-search                 → Vector similarity search (Phase 3)
POST /chat                            → Natural language query with BYOK key (Phase 4)
```

### Topology endpoint spec (critical path — needed for Phase 2.3)

This is the endpoint that replaces all client-side matching logic.

```
POST /products/{name}/topology

Request:
{
  "servers": [
    {
      "name": "VBR Appliance",
      "services": ["Backup server"]
    },
    {
      "name": "Linux Proxy",
      "services": ["Backup proxy (Linux)"]
    }
  ],
  "options": {
    "include_loopback": false,       // default: false
    "include_unresolved": false      // default: false — only connections between defined servers
  }
}

Response:
{
  "product": "VBR v13",
  "servers": [
    {
      "id": "uuid",
      "sourceServer": "VBR Appliance",
      "totalMappedPorts": 21,
      "totalMappedInboundPorts": 5,
      "totalMappedServers": 4,
      "mappedPorts": [...],           // Same structure as current _build_app_import output
      "allInboundPortsTcp": [...],
      "allOutboundPortsTcp": [...],
      "allInboundPortsUdp": [...],
      "allOutboundPortsUdp": [...],
      "mappedPortsByProtocol": [...],
      "mappedPortsByProtocolInbound": [...]
    }
  ],
  "metadata": {
    "total_entries_matched": 41,
    "total_entries_skipped": 252,
    "enrichment_version": "2025-02-16",
    "unresolved_services": ["WAN accelerator", "Tape server"]  // helpful for debugging
  }
}
```

The response body under `servers` is intentionally identical to the current `_build_app_import` output — this means the frontend import format doesn't change at all. The MCP just passes the `servers` array straight to the file writer. The `metadata` block is for debugging and can be omitted from the import file.

### Enriched ports endpoint spec

```
GET /products/{name}/enriched-ports

Response:
[
  {
    "sourceService": "Backup server",
    "targetService": "Backup proxy (Linux)",
    "port": "22",
    "protocol": "TCP",
    "description": "Default SSH port used as a control channel.",
    "subheading": "Backup Server",
    "subheadingL2": null,
    "source_meta": {
      "canonical": "backup server",
      "roles": ["backup server"],
      "os": null,
      "hypervisor": null,
      "storage_type": null
    },
    "target_meta": {
      "canonical": "backup proxy",
      "roles": ["backup proxy"],
      "os": "linux",
      "hypervisor": null,
      "storage_type": null
    }
  }
]
```

---

## Frontend Compatibility

### Will this break the current frontend?

**No.** The changes are entirely additive.

The frontend currently makes standard API calls to the existing endpoints for manual port mapping creation. Those endpoints (`/products/{name}/ports`, `/sourceDetails`, `/search`, etc.) are unchanged — same request format, same response format, same URLs.

The enriched data lives on new endpoints (`/enriched-ports`, `/topology`, `/components`). The frontend doesn't call these unless you build new features that use them.

The import JSON structure (what the frontend imports from file) is also unchanged — the `/topology` endpoint returns the exact same `servers` array shape that `_build_app_import` currently produces. An import file generated by the old MCP or the new backend endpoint are indistinguishable to the frontend.

### Frontend enhancement path (optional, not required)

Once the backend has the topology endpoint, the frontend could call it directly instead of requiring a file import:

1. **Server builder UI** — user picks a product, defines their servers and services (dropdown populated from `/products/{name}/components`), hits "Generate Topology"
2. **Frontend calls `POST /topology`** directly — no MCP, no file export/import dance
3. **Result renders immediately** in the existing port mapping view

This is a natural evolution but entirely optional. The file import flow continues to work indefinitely.

---

## Phase 4: Chat Interface with BYOK LLM Key

### Objective

Add a natural language chat interface to the frontend where users can ask questions about port requirements in plain English. Uses a "bring your own key" model to keep costs on the user, not you.

### Why BYOK

This is a personal project and LLM API costs scale with usage. BYOK means:
- Zero ongoing LLM cost to you (the project owner)
- Users who want chat provide their own API key
- All non-chat features work without a key (enrichment uses your key at scrape time, which is a fixed, negligible cost)
- No need for usage tiers, billing, or rate limiting on your side

### Architecture

```
┌──────────────┐     ┌─────────────────┐     ┌──────────────┐
│   Frontend   │────▶│  Backend /chat   │────▶│  LLM Provider│
│  (chat UI)   │     │  (proxy + RAG)   │     │  (user's key)│
└──────────────┘     └─────────────────┘     └──────────────┘
                            │
                            ▼
                     ┌──────────────┐
                     │ Vector store │  ← context retrieval
                     │ + Graph DB   │
                     └──────────────┘
```

The backend acts as a proxy: it receives the user's question, retrieves relevant context from the vector store and graph, constructs a prompt, and forwards it to the LLM provider using the user's API key. The key never touches the frontend beyond the initial settings input.

### k3s Deployment Changes (Phase 4)

**Traefik streaming timeouts.** Streaming SSE responses from the `/chat` endpoint through `Traefik → backend:8001` will hit default read timeout limits on long LLM responses. Add an `IngressRoute` middleware with extended timeouts for the chat endpoint:

```yaml
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: chat-timeout
  namespace: veeam-ports
spec:
  headers:
    customResponseHeaders:
      X-Content-Type-Options: "nosniff"
---
apiVersion: traefik.io/v1alpha1
kind: IngressRoute
metadata:
  name: veeam-ports-chat
  namespace: veeam-ports
spec:
  entryPoints:
    - websecure
  routes:
    - match: Host(`magicports.veeambp.com`) && PathPrefix(`/ports_server/chat`)
      kind: Rule
      services:
        - name: veeam-ports-backend
          port: 8001
      middlewares:
        - name: chat-timeout
  # Also set readTimeout on the ServersTransport if needed
```

Verify that Traefik's `respondingTimeouts.readTimeout` is sufficient (default 0 = no timeout, but check your config). Test with a slow LLM response (e.g. a complex multi-turn query via Haiku) to confirm the stream isn't cut off.

**No new secrets needed.** User API keys are session-only (in-memory on the backend). The backend doesn't store or need any LLM provider keys for chat — it proxies using the user's key.

---

### Issue 4.1: BYOK key management

**Type:** Implementation

**Depends on:** None (can be built early)

**Description:**
Add a simple key management system to the backend. Users provide their LLM API key, it's stored (encrypted at rest), and used for chat requests. Support multiple providers.

**Key decisions:**
- **Storage:** Encrypted in the database, or session-only (never persisted — user re-enters each session). Session-only is simpler and avoids key custody concerns. Start with session-only.
- **Supported providers:** Anthropic (Claude) and OpenAI as the initial two. Both use similar request/response shapes.
- **Validation:** On key submission, make a minimal API call (e.g. list models) to verify the key works. Return a clear error if invalid.
- **Frontend:** A settings panel where the user pastes their API key, selects provider, and sees a "connected" / "invalid" status.

**API:**

```
POST /settings/llm-key
{
  "provider": "anthropic",        // or "openai"
  "api_key": "sk-ant-..."
}

Response:
{
  "valid": true,
  "provider": "anthropic",
  "model": "claude-sonnet-4-20250514"  // default model for this provider
}
```

The key is stored in-memory on the backend (session-scoped, tied to a session token returned in the response). Not persisted to database in v1.

**Acceptance criteria:**
- Keys validated on submission
- Keys stored session-only (not persisted)
- Support for Anthropic and OpenAI providers
- Clear error messages for invalid keys
- Frontend settings UI for key management

---

### Issue 4.2: Chat endpoint with RAG context

**Type:** Implementation

**Depends on:** 4.1, 3.1 (vector store), 2.2 (graph)

**Description:**
Build the `/chat` endpoint that accepts a natural language question, retrieves relevant context from the enriched data / vector store / graph, constructs a prompt, and proxies the request to the user's LLM provider.

**Request flow:**

1. User sends question via frontend chat UI
2. Backend receives question + session token (which maps to their LLM key)
3. Backend retrieves relevant context:
   - Vector search over enriched port entries (top 10 most relevant)
   - If a product/server context is set, also pull the graph neighbourhood
4. Backend constructs a system prompt with the retrieved context
5. Backend forwards to the user's LLM provider with their key
6. Stream the response back to the frontend

**API:**

```
POST /chat
{
  "message": "What ports do I need between my Linux backup proxy and ESXi hosts?",
  "session_token": "...",
  "context": {
    "product": "VBR v13",          // optional — scopes the search
    "servers": [...]               // optional — if they've already defined their topology
  },
  "history": [                     // optional — conversation history for multi-turn
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ]
}

Response: (streamed)
{
  "response": "For a Linux backup proxy connecting to ESXi hosts, you need...",
  "sources": [                     // citations back to port entries
    {"sourceService": "Backup proxy", "targetService": "ESXi host", "port": "443", ...},
    {"sourceService": "Backup proxy", "targetService": "ESXi host", "port": "902", ...}
  ]
}
```

**System prompt (draft):**

```
You are a Veeam network port requirements assistant. Answer questions about
which firewall ports need to be opened between Veeam Backup & Replication
components.

Use ONLY the port data provided in the context below. Do not invent ports
or guess. If the context doesn't contain the answer, say so.

When listing ports, always include: source component, target component,
port number, protocol, and a brief description of what the port is used for.

Context:
{retrieved_port_entries}

{graph_neighbourhood if available}
```

**Acceptance criteria:**
- Answers grounded in actual port data (no hallucinated ports)
- Citations link back to specific port entries
- Works with Anthropic and OpenAI keys
- Streaming response for good UX
- Multi-turn conversation support
- Graceful handling when no LLM key is configured (returns a clear message, doesn't error)

---

### Issue 4.3: Frontend chat UI

**Type:** Implementation

**Depends on:** 4.2

**Description:**
Add a chat panel to the frontend where users can ask questions about port requirements. Minimal but functional — a message input, streaming response display, and source citations.

**Key design decisions:**
- **Placement:** Slide-out panel or dedicated tab, not a modal (users need to see the port mapping while chatting)
- **State:** If no LLM key is configured, show a prompt to set one in settings. Don't hide the chat feature entirely.
- **Citations:** Clickable — tapping a cited port entry highlights it in the main port mapping view (if applicable)
- **History:** Kept in-memory for the session. No persistence needed in v1.
- **Product context:** If the user is viewing a specific product's ports, automatically scope the chat to that product.

**Acceptance criteria:**
- Chat panel accessible from the main UI
- Streaming response display
- Source citations shown below each response
- Graceful UX when no LLM key is set
- Product context automatically passed when viewing a specific product

---

## Implementation Order and Dependencies

```
Issue 1.1 (Schema)                                          ← Backend repo
  └──▶ Issue 1.2 (LLM enrichment module)                    ← Backend repo
         └──▶ Issue 1.3 (Integrate into scraper)             ← Backend repo
                ├──▶ Issue 2.1 (Graph schema design)         ← Backend repo
                │      └──▶ Issue 2.2 (Build graph)          ← Backend repo
                │             └──▶ Issue 2.3 (Topology endpoint + MCP rewrite)
                │                    │  ↑ Backend: POST /topology endpoint
                │                    │  ↑ MCP: replace _build_app_import with API call
                │                    │  ↑ MCP: delete _find_server + all _has_conflicting_*
                │                    └──▶ Issue 2.4 (Version diff / impact analysis) ← Backend
                ├──▶ Issue 3.1 (Embeddings + sqlite-vec)     ← Backend repo
                │      └──▶ Issue 3.2 (Semantic search tool) ← Backend + MCP
                └──▶ Issue 4.1 (BYOK key management)         ← Backend repo
                       └──▶ Issue 4.2 (Chat endpoint + RAG)  ← Backend repo
                              └──▶ Issue 4.3 (Frontend chat UI) ← Frontend

Note: Issue 1.4 (Simplify _find_server) is SKIPPED.
The MCP's existing matching code stays as-is until Issue 2.3 deletes it entirely.
```

**Recommended execution:**
1. Issues 1.1–1.3 first (biggest impact, solves the data quality problem, all backend)
2. Issues 2.1–2.3 next (backend topology endpoint + MCP gutted to a thin API client)
3. Issue 4.1 can start in parallel with Phase 2 (no dependencies beyond basic backend)
4. Issues 2.4, 3.1–3.2 as stretch goals
5. Issues 4.2–4.3 last (depends on vector store + graph + BYOK being in place)

---

## Cost Analysis

This is a personal project so cost control matters. Here's where money gets spent and how to keep it minimal.

### Your costs (fixed, negligible)

| What | When | Estimated cost |
|---|---|---|
| LLM enrichment (Haiku) | Once per scrape (~300 entries × ~500 tokens) | < $0.01 per scrape |
| Embeddings (Phase 3) | Once per scrape (~300 entries) | < $0.01 per scrape |
| k3s hosting | Already running | $0 incremental |
| SQLite graph (Phase 2) | In-process, no new infra | $0 |

Total ongoing cost to you: effectively zero. The enrichment runs on your existing scrape schedule (daily? weekly?) and costs fractions of a penny per run.

### User costs (BYOK, only for chat)

| What | Who pays | Estimated cost |
|---|---|---|
| Chat queries (Phase 4) | User's own API key | ~$0.003 per query (Haiku) to ~$0.03 (Sonnet) |
| All non-chat features | Nobody — no LLM needed at query time | $0 |

The entire port lookup, topology generation, and search workflow works without any LLM key. Chat is the only feature that requires one, and the user provides their own.

### Infrastructure decisions driven by cost

- **SQLite over Neo4j** for Phase 2 — no new database to host. The graph is small (~300 nodes, ~500 edges per product). SQLite adjacency tables are more than sufficient.
- **`sqlite-vec` over Qdrant/pgvector** for Phase 3 — keeps everything in one SQLite file. No sidecar, no new PVC, no new deployment. Falls through the atomic swap pipeline from Phase 2.
- **Session-only key storage** for BYOK — avoids encryption-at-rest complexity and key custody liability.
- **Haiku for enrichment** — cheapest model, more than capable for structured extraction. Don't use Sonnet or Opus for this.
- **Local embeddings over API embeddings** (Phase 3) — `sentence-transformers` runs on CPU and avoids another API key dependency. The dataset is small enough (~300 entries) that inference time is negligible.
- **Atomic file swap for deploys** — build the entire database (raw + enriched + graph + vectors) to a staging file, swap on success. No concurrent access concerns, no WAL complexity, no PVC access mode issues.

---

## Test Products

Use these configurations for end-to-end testing across all phases:

**Config A — Linux + VMware (primary test):**
```json
[
  {"name": "VBR Appliance", "services": ["Backup server"]},
  {"name": "Linux Proxy", "services": ["Backup proxy (Linux)"]},
  {"name": "Linux Repository", "services": ["Backup repository (Linux)"]},
  {"name": "ESXi Host", "services": ["ESXi host"]},
  {"name": "vCenter Server", "services": ["vCenter Server"]}
]
```

**Config B — Windows + VMware:**
```json
[
  {"name": "VBR Server", "services": ["Backup server"]},
  {"name": "Windows Proxy", "services": ["Backup proxy (Microsoft Windows)"]},
  {"name": "Windows Repository", "services": ["Backup repository (Microsoft Windows)"]},
  {"name": "ESXi Host", "services": ["ESXi host"]},
  {"name": "vCenter Server", "services": ["vCenter Server"]}
]
```

**Config C — Linux + NFS + VMware:**
```json
[
  {"name": "VBR Appliance", "services": ["Backup server"]},
  {"name": "Linux Proxy", "services": ["Backup proxy (Linux)"]},
  {"name": "NFS Repository", "services": ["NFS backup repository"]},
  {"name": "ESXi Host", "services": ["ESXi host"]},
  {"name": "vCenter Server", "services": ["vCenter Server"]}
]
```

**Config D — Windows + Hyper-V:**
```json
[
  {"name": "VBR Server", "services": ["Backup server"]},
  {"name": "Windows Proxy", "services": ["Backup proxy (Microsoft Windows)"]},
  {"name": "Windows Repository", "services": ["Backup repository (Microsoft Windows)"]},
  {"name": "Hyper-V Host", "services": ["Hyper-V host"]}
]
```

**Validation rules for all configs:**
- No NFS/SMB/Object Storage entries unless the config explicitly includes them
- No Hyper-V entries in VMware configs and vice versa
- No Windows entries in Linux configs and vice versa
- No loopback connections (source server == target server)
- Only connections between user-defined servers (no unresolved targets)
- Port counts should be plausible (single digits to low tens per server, not hundreds)
