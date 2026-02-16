"""
Generate and store vector embeddings for enriched port data.

Reads enriched_ports, generates embeddings using fastembed (ONNX Runtime),
and stores them in a sqlite-vec virtual table for semantic search.
"""

import logging
import sqlite3
import time

import numpy as np

log = logging.getLogger(__name__)

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

# Lazy singleton for the embedding model
_model = None


def _get_model():
    """Get or create the fastembed TextEmbedding model (cached)."""
    global _model
    if _model is None:
        from fastembed import TextEmbedding

        _model = TextEmbedding(model_name=MODEL_NAME)
        log.info("Loaded embedding model: %s", MODEL_NAME)
    return _model


def build_embeddings(conn: sqlite3.Connection) -> None:
    """Build vector embeddings from enriched_ports data.

    Called from scrape_ports.py after graph construction, before commit.
    """
    start = time.monotonic()

    _load_sqlite_vec(conn)
    _create_embedding_table(conn)

    rows = _load_enriched_rows(conn)
    if not rows:
        log.warning("No enriched_ports rows found, skipping embedding generation")
        return

    texts = [_build_embedding_text(row) for row in rows]
    log.info("Generating embeddings for %d rows...", len(texts))

    model = _get_model()
    embeddings = list(model.embed(texts))

    _insert_embeddings(conn, rows, embeddings)

    elapsed = time.monotonic() - start
    log.info("Embeddings built in %.2fs", elapsed)


def embed_query(text: str) -> bytes:
    """Embed a single query string and return as serialized float32 bytes.

    Called by the server at request time for semantic search.
    """
    model = _get_model()
    embedding = list(model.embed([text]))[0]
    return np.asarray(embedding, dtype=np.float32).tobytes()


def _load_sqlite_vec(conn: sqlite3.Connection) -> None:
    """Load the sqlite-vec extension into the connection."""
    import sqlite_vec

    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def _create_embedding_table(conn: sqlite3.Connection) -> None:
    """Drop and recreate the port_embeddings virtual table."""
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS port_embeddings")
    cur.execute(f"""
        CREATE VIRTUAL TABLE port_embeddings USING vec0(
            enriched_rowid INTEGER PRIMARY KEY,
            embedding float[{EMBEDDING_DIM}] distance_metric=cosine,
            product text partition key
        )
    """)


def _load_enriched_rows(conn: sqlite3.Connection) -> list[dict]:
    """Load all enriched_ports rows with their rowids."""
    cur = conn.cursor()
    cur.execute("""
        SELECT rowid, product, source_canonical, target_canonical,
               port, protocol, description,
               source_os, source_hypervisor, source_storage_type,
               target_os, target_hypervisor, target_storage_type
        FROM enriched_ports
    """)
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _build_embedding_text(row: dict) -> str:
    """Construct the text string to embed for a single enriched_ports row."""
    parts = [
        f"[{row['product']}]",
        f"{row['source_canonical']} -> {row['target_canonical']}",
        f"| Port: {row['port']} {row['protocol']}",
    ]

    desc = row.get("description")
    if desc:
        parts.append(f"| {desc}")

    qualifiers = []
    src_os = row.get("source_os")
    tgt_os = row.get("target_os")
    if src_os or tgt_os:
        os_str = "/".join(filter(None, [src_os, tgt_os]))
        qualifiers.append(f"OS: {os_str}")

    src_hyp = row.get("source_hypervisor")
    tgt_hyp = row.get("target_hypervisor")
    if src_hyp or tgt_hyp:
        hyp_str = "/".join(filter(None, [src_hyp, tgt_hyp]))
        qualifiers.append(f"Hypervisor: {hyp_str}")

    src_st = row.get("source_storage_type")
    tgt_st = row.get("target_storage_type")
    if src_st or tgt_st:
        st_str = "/".join(filter(None, [src_st, tgt_st]))
        qualifiers.append(f"Type: {st_str}")

    if qualifiers:
        parts.append("| " + " | ".join(qualifiers))

    return " ".join(parts)


def _insert_embeddings(
    conn: sqlite3.Connection,
    rows: list[dict],
    embeddings: list,
) -> None:
    """Insert all embeddings into the port_embeddings virtual table."""
    cur = conn.cursor()
    params = [
        (row["rowid"], np.asarray(emb, dtype=np.float32).tobytes(), row["product"])
        for row, emb in zip(rows, embeddings)
    ]
    cur.executemany(
        "INSERT INTO port_embeddings(enriched_rowid, embedding, product) VALUES (?, ?, ?)",
        params,
    )
    log.info("Inserted %d embeddings", len(rows))
