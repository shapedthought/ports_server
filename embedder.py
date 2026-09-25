"""
Generate and store vector embeddings for enriched port data.

Uses Voyage AI HTTP embeddings (voyage-4-lite @ 512 dims by default) so the
VPS never runs a local ONNX model. Index builds use input_type=document;
query embeds use input_type=query. Stores vectors in a sqlite-vec virtual
table for semantic search.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import datetime, timezone

import numpy as np
import requests

log = logging.getLogger(__name__)

VOYAGE_API_URL = "https://api.voyageai.com/v1/embeddings"
DEFAULT_MODEL = "voyage-4-lite"
DEFAULT_DIMENSIONS = 512
BATCH_SIZE = 128  # Voyage allows up to 1000 inputs per request

MODEL_NAME = os.environ.get("VOYAGE_MODEL", DEFAULT_MODEL)
EMBEDDING_DIM = int(os.environ.get("VOYAGE_DIMENSIONS", str(DEFAULT_DIMENSIONS)))


def _require_api_key() -> str:
    key = os.environ.get("VOYAGE_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "VOYAGE_API_KEY is required for Voyage embeddings. "
            "Set the env var or use keyword fallback / --skip-vectors."
        )
    return key


def _embed_texts(texts: list[str], input_type: str) -> list[np.ndarray]:
    """Embed texts via Voyage AI HTTP API. Returns list of float32 ndarrays."""
    if not texts:
        return []

    api_key = _require_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    all_embeddings: list[np.ndarray] = []
    for start in range(0, len(texts), BATCH_SIZE):
        batch = texts[start : start + BATCH_SIZE]
        payload = {
            "input": batch,
            "model": MODEL_NAME,
            "input_type": input_type,
            "output_dimension": EMBEDDING_DIM,
        }
        resp = requests.post(VOYAGE_API_URL, headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        data = resp.json()

        # Voyage returns data sorted by index; sort defensively anyway.
        items = sorted(data["data"], key=lambda d: d["index"])
        if len(items) != len(batch):
            raise RuntimeError(
                f"Voyage returned {len(items)} embeddings for {len(batch)} inputs"
            )
        for item in items:
            vec = np.asarray(item["embedding"], dtype=np.float32)
            if vec.shape != (EMBEDDING_DIM,):
                raise RuntimeError(
                    f"Expected embedding dim {EMBEDDING_DIM}, got {vec.shape}"
                )
            all_embeddings.append(vec)

        log.info(
            "Embedded batch %d-%d / %d (model=%s, dims=%d, input_type=%s)",
            start + 1,
            start + len(batch),
            len(texts),
            MODEL_NAME,
            EMBEDDING_DIM,
            input_type,
        )

    return all_embeddings


def build_embeddings(conn: sqlite3.Connection) -> None:
    """Build vector embeddings from enriched_ports data.

    Called from scrape_ports.py after graph construction, before commit.
    Uses Voyage input_type=document.
    """
    start = time.monotonic()

    _load_sqlite_vec(conn)
    _create_embedding_table(conn)

    rows = _load_enriched_rows(conn)
    if not rows:
        log.warning("No enriched_ports rows found, skipping embedding generation")
        return

    texts = [_build_embedding_text(row) for row in rows]
    log.info(
        "Generating Voyage embeddings for %d rows (model=%s, dims=%d)...",
        len(texts),
        MODEL_NAME,
        EMBEDDING_DIM,
    )

    embeddings = _embed_texts(texts, input_type="document")
    _insert_embeddings(conn, rows, embeddings)
    _write_embeddings_meta(conn)

    elapsed = time.monotonic() - start
    log.info(
        "Embeddings built in %.2fs (model=%s, dims=%d, count=%d)",
        elapsed,
        MODEL_NAME,
        EMBEDDING_DIM,
        len(rows),
    )


def embed_query(text: str) -> bytes:
    """Embed a single query string and return as serialized float32 bytes.

    Called by the server at request time for semantic search.
    Uses Voyage input_type=query.
    """
    embedding = _embed_texts([text], input_type="query")[0]
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


def _write_embeddings_meta(conn: sqlite3.Connection) -> None:
    """Persist light ops metadata about the last embedding build."""
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS embeddings_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            model TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            built_at TEXT NOT NULL
        )
    """)
    cur.execute(
        """
        INSERT INTO embeddings_meta (id, model, dimensions, built_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            model = excluded.model,
            dimensions = excluded.dimensions,
            built_at = excluded.built_at
        """,
        (MODEL_NAME, EMBEDDING_DIM, datetime.now(timezone.utc).isoformat()),
    )


def _load_enriched_rows(conn: sqlite3.Connection) -> list[dict]:
    """Load deduplicated enriched_ports rows with their rowids.

    Groups by (product, source_canonical, target_canonical, port, protocol)
    to avoid embedding the same port rule multiple times when it appears
    under different subheadings. Concatenates distinct subheadings for context.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT MIN(rowid) as rowid, product, source_canonical, target_canonical,
               port, protocol, description,
               source_os, source_hypervisor, source_storage_type,
               target_os, target_hypervisor, target_storage_type,
               GROUP_CONCAT(DISTINCT subheading) as subheadings
        FROM enriched_ports
        GROUP BY product, source_canonical, target_canonical, port, protocol
    """)
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def _build_embedding_text(row: dict) -> str:
    """Construct the text string to embed for a single enriched_ports row."""
    subheadings = row.get("subheadings") or ""
    parts = [
        f"[{row['product']}]",
        f"[{subheadings}]" if subheadings else "",
        f"{row['source_canonical']} -> {row['target_canonical']}",
        f"| Port: {row['port']} {row['protocol']}",
    ]
    parts = [p for p in parts if p]

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
