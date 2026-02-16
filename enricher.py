"""
LLM-based enrichment pipeline for Veeam port data.

Extracts structured metadata (canonical names, roles, OS, hypervisor,
storage type) from raw service names using Claude Haiku at scrape time.
"""

import json
import logging
import sqlite3
import time
from datetime import date
from typing import Any

import anthropic
import pandas as pd
from pydantic import ValidationError

from models import ServiceMeta

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_BATCH_SIZE = 25
MAX_RETRIES = 2

SYSTEM_PROMPT = """\
You are a data normalisation assistant for Veeam Backup & Replication network port data.

For each service name in the input list, extract structured metadata.

Rules:
- canonical: Base service name with ALL qualifiers removed, lowercased. \
Remove OS qualifiers, hypervisor prefixes, storage type prefixes, \
direction qualifiers (source/target), DNS names in parentheses, \
version numbers. Examples:
    "Backup proxy (Linux)" -> "backup proxy"
    "NFS backup repository" -> "backup repository"
    "ESXi host (source)" -> "esxi host"
    "Veeam Update Repository (repository.veeam.com)" -> "veeam update repository"
    "vCenter Server" -> "vcenter server"
    "VM guest OS (Microsoft Windows)" -> "vm guest os"
    "SMB (CIFS) backup repository (Microsoft Windows)" -> "backup repository"
    "Hyper-V server/Off-host backup proxy" -> "backup proxy"
    "CDP proxy (source and target)" -> "cdp proxy"

- roles: ALL functional roles this service fulfils. Split on "or", "/", \
and comma-separated lists. Lowercased. Most entries have a single role.
    "Gateway server or backup proxy" -> ["gateway server", "backup proxy"]
    "Backup proxy (direct connection)/Gateway server or backup server" -> \
["backup proxy", "gateway server", "backup server"]
    "Hyper-V server/Off-host backup proxy" -> ["hyper-v server", "backup proxy"]
    Single role: "Backup server" -> ["backup server"]

- os: Extract if present. "linux" | "windows" | null.
    "Linux", "Linux/Unix" -> "linux"
    "Microsoft Windows", "Windows" -> "windows"
    null if not specified.

- hypervisor: Extract if present. "vmware" | "hyperv" | "nutanix" | null.
    "ESXi", "vCenter", "VMware" -> "vmware"
    "Hyper-V", "SCVMM" -> "hyperv"
    "Nutanix AHV" -> "nutanix"
    null if not specified.

- storage_type: Extract if present. "nfs" | "smb" | "object_storage" | "hardened" | null.
    "NFS" -> "nfs"
    "SMB", "CIFS", "SMB (CIFS)" -> "smb"
    "Object storage", "S3" -> "object_storage"
    "Hardened" -> "hardened"
    null if not specified.

- original: The EXACT original service name from the input, unchanged.

Respond with ONLY a JSON array of objects. No markdown fences, no commentary. \
One object per input service name, in the same order as the input."""


def enrich_service_names(
    service_names: list[str],
    api_key: str,
    batch_size: int = DEFAULT_BATCH_SIZE,
    model: str = DEFAULT_MODEL,
) -> dict[str, ServiceMeta]:
    """Enrich a list of unique service names via LLM.

    Returns a dict mapping each raw service name to its ServiceMeta.
    """
    client = anthropic.Anthropic(api_key=api_key)
    lookup: dict[str, ServiceMeta] = {}

    # Process in batches
    total = len(service_names)
    for i in range(0, total, batch_size):
        batch = service_names[i : i + batch_size]
        batch_num = i // batch_size + 1
        total_batches = (total + batch_size - 1) // batch_size
        log.info("Enriching batch %d/%d (%d names)", batch_num, total_batches, len(batch))

        results = _enrich_batch(batch, client, model)
        for name, meta in zip(batch, results):
            lookup[name] = meta

    log.info("Enrichment complete: %d service names processed", len(lookup))
    return lookup


def write_enriched_table(
    conn: sqlite3.Connection,
    df: pd.DataFrame,
    enrichment: dict[str, ServiceMeta],
    enrichment_version: str | None = None,
) -> None:
    """Create the enriched_ports table and populate it from the DataFrame + enrichment lookup."""
    if enrichment_version is None:
        enrichment_version = date.today().isoformat()

    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS enriched_ports")
    cur.execute("""
        CREATE TABLE enriched_ports (
            subheading TEXT,
            subheadingL2 TEXT,
            subheadingL3 TEXT,
            product TEXT,
            sourceService TEXT,
            targetService TEXT,
            protocol TEXT,
            port TEXT,
            description TEXT,
            source_canonical TEXT,
            source_roles TEXT,
            source_os TEXT,
            source_hypervisor TEXT,
            source_storage_type TEXT,
            target_canonical TEXT,
            target_roles TEXT,
            target_os TEXT,
            target_hypervisor TEXT,
            target_storage_type TEXT,
            enrichment_version TEXT
        )
    """)

    rows = []
    for _, row in df.iterrows():
        source_name = row["sourceService"]
        target_name = row["targetService"]

        source_meta = enrichment.get(source_name, _fallback_meta(source_name))
        target_meta = enrichment.get(target_name, _fallback_meta(target_name))

        rows.append((
            row["subheading"],
            row["subheadingL2"],
            row["subheadingL3"],
            row["product"],
            row["sourceService"],
            row["targetService"],
            row["protocol"],
            row["port"],
            row["description"],
            source_meta.canonical,
            json.dumps(source_meta.roles),
            source_meta.os,
            source_meta.hypervisor,
            source_meta.storage_type,
            target_meta.canonical,
            json.dumps(target_meta.roles),
            target_meta.os,
            target_meta.hypervisor,
            target_meta.storage_type,
            enrichment_version,
        ))

    cur.executemany(
        "INSERT INTO enriched_ports VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    log.info("Wrote %d enriched rows", len(rows))


def _enrich_batch(
    batch: list[str],
    client: anthropic.Anthropic,
    model: str = DEFAULT_MODEL,
    max_retries: int = MAX_RETRIES,
) -> list[ServiceMeta]:
    """Send a batch of service names to the LLM and return enriched metadata."""
    text = ""
    for attempt in range(max_retries + 1):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_user_message(batch)}],
            )
            text = response.content[0].text.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                # Remove opening fence (```json or ```)
                text = text.split("\n", 1)[1] if "\n" in text else text[3:]
                # Remove closing fence
                if text.endswith("```"):
                    text = text[: -3].strip()
            log.debug("Raw LLM response (first 200 chars): %s", text[:200])
            parsed = json.loads(text)
            return _validate_and_fix(parsed, batch)
        except (json.JSONDecodeError, ValidationError, KeyError, IndexError) as e:
            log.warning("Enrichment attempt %d/%d failed: %s", attempt + 1, max_retries + 1, e)
            log.warning("Raw response (first 300 chars): %s", repr(text[:300]))
            if attempt == max_retries:
                log.error("Enrichment failed after %d attempts, using fallback", max_retries + 1)
                return [_fallback_meta(name) for name in batch]
        except anthropic.APIError as e:
            log.warning("Anthropic API error on attempt %d/%d: %s", attempt + 1, max_retries + 1, e)
            if attempt == max_retries:
                log.error("API error persisted, using fallback for batch")
                return [_fallback_meta(name) for name in batch]
            time.sleep(2**attempt)

    return [_fallback_meta(name) for name in batch]


def _build_user_message(batch: list[str]) -> str:
    """Format a batch of service names into the user message."""
    numbered = "\n".join(f"{i + 1}. {name}" for i, name in enumerate(batch))
    return f"Extract structured metadata for these {len(batch)} service names:\n\n{numbered}"


def _validate_and_fix(
    raw_response: list[dict[str, Any]],
    original_names: list[str],
) -> list[ServiceMeta]:
    """Validate LLM output against ServiceMeta schema and fix common issues."""
    if len(raw_response) != len(original_names):
        raise ValueError(
            f"Response count mismatch: got {len(raw_response)}, expected {len(original_names)}"
        )

    results = []
    for item, original_name in zip(raw_response, original_names):
        # Ensure original field matches the actual input
        item["original"] = original_name

        # Normalise None-like strings to actual None, and flatten lists to first value
        for field in ("os", "hypervisor", "storage_type"):
            val = item.get(field)
            if isinstance(val, list):
                val = val[0] if val else None
            if val in (None, "null", "none", "None", ""):
                val = None
            item[field] = val

        # Ensure roles is a list
        if isinstance(item.get("roles"), str):
            item["roles"] = [item["roles"]]

        results.append(ServiceMeta(**item))

    return results


def _fallback_meta(service_name: str) -> ServiceMeta:
    """Return a minimal ServiceMeta when LLM enrichment fails."""
    return ServiceMeta(
        canonical=service_name.lower().strip(),
        roles=[service_name.lower().strip()] if service_name.strip() else [],
        os=None,
        hypervisor=None,
        storage_type=None,
        original=service_name,
    )
