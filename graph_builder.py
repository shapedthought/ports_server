"""
Build a knowledge graph from enriched port data.

Reads the enriched_ports table and creates three graph tables:
- components: deduplicated service nodes with canonical name + qualifiers
- connections: directed edges between components with port/protocol metadata
- component_variants: links specific (qualified) components to their generic base
"""

import logging
import sqlite3
import time

from synonyms import SYNONYM_CANONICAL as _SYNONYM_CANONICAL

log = logging.getLogger(__name__)


def _normalize_canonical(name: str) -> str:
    """Map synonym canonical names to a single representative."""
    return _SYNONYM_CANONICAL.get(name.lower(), name)


def build_graph(conn: sqlite3.Connection) -> None:
    """Build the knowledge graph from enriched_ports data.

    Called from scrape_ports.py after enrichment, before commit.
    """
    start = time.monotonic()
    cur = conn.cursor()

    _create_graph_tables(cur)
    component_lookup = _extract_components(cur)
    _extract_connections(cur, component_lookup)
    _build_variant_links(cur)

    elapsed = time.monotonic() - start
    log.info("Graph built in %.2fs", elapsed)


def _create_graph_tables(cur: sqlite3.Cursor) -> None:
    """Drop and recreate all graph tables with indexes."""
    cur.execute("DROP TABLE IF EXISTS component_variants")
    cur.execute("DROP TABLE IF EXISTS connections")
    cur.execute("DROP TABLE IF EXISTS components")

    cur.execute("""
        CREATE TABLE components (
            id INTEGER PRIMARY KEY,
            product TEXT NOT NULL,
            canonical TEXT NOT NULL,
            os TEXT,
            hypervisor TEXT,
            storage_type TEXT,
            original_name TEXT,
            UNIQUE(product, canonical, os, hypervisor, storage_type)
        )
    """)

    cur.execute("""
        CREATE TABLE connections (
            id INTEGER PRIMARY KEY,
            product TEXT NOT NULL,
            source_component_id INTEGER REFERENCES components(id),
            target_component_id INTEGER REFERENCES components(id),
            port TEXT NOT NULL,
            protocol TEXT NOT NULL,
            description TEXT,
            UNIQUE(product, source_component_id, target_component_id, port, protocol)
        )
    """)

    cur.execute("""
        CREATE TABLE component_variants (
            id INTEGER PRIMARY KEY,
            specific_id INTEGER REFERENCES components(id),
            generic_id INTEGER REFERENCES components(id),
            UNIQUE(specific_id, generic_id)
        )
    """)

    cur.execute("CREATE INDEX idx_components_product_canonical ON components(product, canonical)")
    cur.execute("CREATE INDEX idx_connections_source ON connections(source_component_id)")
    cur.execute("CREATE INDEX idx_connections_target ON connections(target_component_id)")
    cur.execute("CREATE INDEX idx_connections_product ON connections(product)")
    cur.execute("CREATE INDEX idx_variants_specific ON component_variants(specific_id)")
    cur.execute("CREATE INDEX idx_variants_generic ON component_variants(generic_id)")


def _extract_components(cur: sqlite3.Cursor) -> dict[tuple, int]:
    """Extract unique components from enriched_ports into the components table.

    Returns a lookup dict mapping (product, canonical, os, hypervisor, storage_type) to component id.
    """
    # Collect unique component tuples from both source and target sides
    cur.execute("""
        SELECT DISTINCT product, source_canonical, source_os, source_hypervisor,
                        source_storage_type, sourceService
        FROM enriched_ports
        WHERE source_canonical IS NOT NULL AND source_canonical != ''
    """)
    source_rows = cur.fetchall()

    cur.execute("""
        SELECT DISTINCT product, target_canonical, target_os, target_hypervisor,
                        target_storage_type, targetService
        FROM enriched_ports
        WHERE target_canonical IS NOT NULL AND target_canonical != ''
    """)
    target_rows = cur.fetchall()

    # Deduplicate: keep first original_name per (product, canonical, os, hyp, storage)
    # Normalize synonyms so e.g. "esxi server" and "esxi host" merge into one component.
    seen: dict[tuple, str] = {}  # key -> first original_name
    for product, canonical, os_val, hyp, storage, original in source_rows + target_rows:
        canonical = _normalize_canonical(canonical)
        key = (product, canonical, _normalize_null(os_val), _normalize_null(hyp), _normalize_null(storage))
        if key not in seen:
            seen[key] = original

    # Insert into components table
    for key, original_name in seen.items():
        product, canonical, os_val, hyp, storage = key
        cur.execute(
            "INSERT OR IGNORE INTO components (product, canonical, os, hypervisor, storage_type, original_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (product, canonical, os_val, hyp, storage, original_name),
        )

    # Build lookup dict from the table (to get assigned IDs)
    cur.execute("SELECT id, product, canonical, os, hypervisor, storage_type FROM components")
    lookup: dict[tuple, int] = {}
    for row in cur.fetchall():
        cid, product, canonical, os_val, hyp, storage = row
        key = (product, canonical, _normalize_null(os_val), _normalize_null(hyp), _normalize_null(storage))
        lookup[key] = cid

    # Log per-product stats
    products = {}
    for key in lookup:
        products[key[0]] = products.get(key[0], 0) + 1
    for product, count in sorted(products.items()):
        log.info("  %s: %d components", product, count)
    log.info("Total components: %d", len(lookup))

    return lookup


def _extract_connections(cur: sqlite3.Cursor, component_lookup: dict[tuple, int]) -> None:
    """Create connections between components from enriched_ports rows."""
    cur.execute("""
        SELECT product, source_canonical, source_os, source_hypervisor, source_storage_type,
               target_canonical, target_os, target_hypervisor, target_storage_type,
               port, protocol, description
        FROM enriched_ports
    """)

    inserted = 0
    skipped = 0
    for row in cur.fetchall():
        (product, s_canon, s_os, s_hyp, s_storage,
         t_canon, t_os, t_hyp, t_storage,
         port, protocol, description) = row

        s_canon = _normalize_canonical(s_canon)
        t_canon = _normalize_canonical(t_canon)
        source_key = (product, s_canon, _normalize_null(s_os), _normalize_null(s_hyp), _normalize_null(s_storage))
        target_key = (product, t_canon, _normalize_null(t_os), _normalize_null(t_hyp), _normalize_null(t_storage))

        source_id = component_lookup.get(source_key)
        target_id = component_lookup.get(target_key)

        if source_id is None or target_id is None:
            skipped += 1
            continue

        try:
            cur.execute(
                "INSERT OR IGNORE INTO connections "
                "(product, source_component_id, target_component_id, port, protocol, description) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (product, source_id, target_id, port, protocol, description),
            )
            if cur.rowcount > 0:
                inserted += 1
        except sqlite3.IntegrityError:
            pass  # UNIQUE constraint — duplicate connection

    log.info("Connections: %d inserted, %d skipped (missing components)", inserted, skipped)


def _build_variant_links(cur: sqlite3.Cursor) -> None:
    """Link specific (qualified) components to their generic (all-NULL qualifiers) base.

    For each component with any non-NULL qualifier, find or create the fully-generic
    version and insert a variant link.
    """
    # Find all components with at least one non-NULL qualifier
    cur.execute("""
        SELECT id, product, canonical, os, hypervisor, storage_type
        FROM components
        WHERE os IS NOT NULL OR hypervisor IS NOT NULL OR storage_type IS NOT NULL
    """)
    specific_components = cur.fetchall()

    links_created = 0
    generics_created = 0

    for specific_id, product, canonical, os_val, hyp, storage in specific_components:
        # Look for the fully-generic version
        cur.execute(
            "SELECT id FROM components "
            "WHERE product = ? AND canonical = ? AND os IS NULL AND hypervisor IS NULL AND storage_type IS NULL",
            (product, canonical),
        )
        row = cur.fetchone()

        if row:
            generic_id = row[0]
        else:
            # Create synthetic generic node
            cur.execute(
                "INSERT INTO components (product, canonical, os, hypervisor, storage_type, original_name) "
                "VALUES (?, ?, NULL, NULL, NULL, ?)",
                (product, canonical, f"{canonical} (generic)"),
            )
            generic_id = cur.lastrowid
            generics_created += 1
            log.debug("Created synthetic generic component: %s / %s", product, canonical)

        # Link specific → generic
        cur.execute(
            "INSERT OR IGNORE INTO component_variants (specific_id, generic_id) VALUES (?, ?)",
            (specific_id, generic_id),
        )
        if cur.rowcount > 0:
            links_created += 1

    if generics_created:
        log.info("Created %d synthetic generic components", generics_created)
    log.info("Variant links: %d created", links_created)


def _normalize_null(value: str | None) -> str | None:
    """Normalize empty strings to None for consistent lookup keys."""
    if value is None or value == "":
        return None
    return value
