"""Search-quality helpers for keyword token matching and semantic disambiguation.

Phase 1 (keyword): token-AND across a multi-field haystack, with ROLE_SYNONYM_GROUPS
phrase expansion (e.g. ESXi host ↔ ESXi server).

Phase 2 (semantic): lightweight query rewrite for bare "proxy" + VMware cues, plus
a mild candidate boost/demote after vector (or keyword-fallback) retrieval.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from synonyms import ROLE_SYNONYM_GROUPS

# --- Phase 1: keyword token AND + synonyms ---------------------------------

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[./][A-Za-z0-9]+)?")


def tokenize_query(query: str) -> list[str]:
    """Split a free-text query into lowercase alphanumeric tokens."""
    return [m.group(0).lower() for m in _TOKEN_RE.finditer(query or "")]


def synonym_query_variants(query: str) -> list[str]:
    """Return lowercase query variants with ROLE_SYNONYM_GROUPS phrase swaps.

    Example: "Backup proxy ESXi host" also yields "... esxi server".
    """
    base = (query or "").strip().lower()
    if not base:
        return []

    variants: set[str] = {base}
    # Iterate to allow chained substitutions across groups (usually one step).
    changed = True
    while changed:
        changed = False
        current = list(variants)
        for text in current:
            for group in ROLE_SYNONYM_GROUPS:
                for phrase in group:
                    if phrase and phrase in text:
                        for alt in group:
                            if alt == phrase:
                                continue
                            swapped = text.replace(phrase, alt)
                            if swapped not in variants:
                                variants.add(swapped)
                                changed = True
    return sorted(variants)


def token_variant_lists(query: str) -> list[list[str]]:
    """AND-of-OR token plans: one plan per synonym query variant.

    Each inner list is the token sequence that must all match (AND).
    Outer list is OR'd across synonym variants.
    """
    plans: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for variant in synonym_query_variants(query):
        tokens = tokenize_query(variant)
        key = tuple(tokens)
        if tokens and key not in seen:
            seen.add(key)
            plans.append(tokens)
    return plans


def build_haystack(parts: Iterable[Any]) -> str:
    """Concatenate row fields into a lowercase searchable haystack."""
    return " ".join(str(p) for p in parts if p is not None and str(p).strip()).lower()


def haystack_matches_query(haystack: str, query: str) -> bool:
    """True if any synonym variant's tokens all appear as substrings in haystack."""
    hay = (haystack or "").lower()
    if not hay or not (query or "").strip():
        return False
    for tokens in token_variant_lists(query):
        if all(tok in hay for tok in tokens):
            return True
    return False


# Columns used for keyword /search across all_ports.
KEYWORD_SEARCH_COLUMNS = (
    "sourceService",
    "targetService",
    "description",
    "port",
    "subheading",
    "subheadingL2",
    "subheadingL3",
    "product",
)

# Columns used for semantic keyword fallback on enriched_ports.
KEYWORD_FALLBACK_COLUMNS = (
    "sourceService",
    "targetService",
    "description",
    "port",
    "source_canonical",
    "target_canonical",
    "product",
)


# --- Phase 2a: query rewrite -----------------------------------------------

_VMWARE_CUES = re.compile(
    r"\b(vmware|vsphere|esxi|vcenter|hypervisor|hyper-?v)\b",
    re.IGNORECASE,
)
_PROXY_WORD = re.compile(r"\bprox(?:y|ies)\b", re.IGNORECASE)
_NAMED_SPECIAL_PROXY = re.compile(
    r"\b("
    r"cdp|"
    r"sure\s*-?\s*backup|surebackup|"
    r"guest(?:\s+interaction)?|"
    r"virtual\s+lab"
    r")\b",
    re.IGNORECASE,
)
_ALREADY_BACKUP_PROXY = re.compile(
    r"\b(backup\s+prox(?:y|ies)|veeam\s+prox(?:y|ies))\b",
    re.IGNORECASE,
)

REWRITE_SUFFIX = "backup proxy veeam proxy"


def detect_backup_proxy_vmware_intent(query: str) -> bool:
    """Bare proxy + VMware/hypervisor cue, without CDP/SureBackup/guest/lab."""
    q = query or ""
    if not _PROXY_WORD.search(q):
        return False
    if not _VMWARE_CUES.search(q):
        return False
    if _NAMED_SPECIAL_PROXY.search(q):
        return False
    return True


def rewrite_proxy_query(query: str) -> tuple[str, dict[str, Any]]:
    """Expand bare proxy+VMware queries toward backup/veeam proxy.

    Returns (possibly rewritten query, intent dict). Intent always includes
    ``backup_proxy_vmware`` (bool) for the boost stage.
    """
    intent: dict[str, Any] = {"backup_proxy_vmware": False}
    q = (query or "").strip()
    if not detect_backup_proxy_vmware_intent(q):
        return q, intent

    intent["backup_proxy_vmware"] = True
    if _ALREADY_BACKUP_PROXY.search(q):
        return q, intent

    rewritten = f"{q} {REWRITE_SUFFIX}".strip()
    intent["rewritten_from"] = q
    return rewritten, intent


# --- Phase 2b: mild candidate boost ----------------------------------------

_PREFERRED_ROLE_FRAGMENTS = (
    "backup proxy",
)

_DEMOTED_ROLE_FRAGMENTS = (
    "cdp proxy",
    "guest interaction proxy",
    "surebackup",
    "sure backup",
    "virtual lab",
)

# Hypervisor / management targets for VMware-intent *target* boost.
# Avoid bare "vmware" (appears on many non-edge rows) and bare "esx"
# (would substring-match inside "esxi").
_HYPERVISOR_TARGET_FRAGMENTS = (
    "esxi",
    "esx server",
    "esx host",
    "vcenter",
    "vsphere",
)

# Same-role data-plane peers that dominate bare proxy+VMware vector hits
# (Backup proxy↔Backup server / repository / storage / gateway on 2500–3300).
# Demoted only under VMware intent when the row is preferred Backup proxy
# but is *not* a hypervisor/management edge.
_PEER_DATA_FRAGMENTS = (
    "backup server",
    "backup repository",
    "repository",
    "object storage",
    "scale-out",
    "gateway server",
)

# Mild deltas kept small so strong vector scores still dominate when clear.
_BOOST_DELTA = 0.15
_DEMOTE_DELTA = 0.12
# Stronger than role boost so Backup proxy→ESXi/vCenter can leap peer data ports.
_TARGET_BOOST_DELTA = 0.30
# Mild peer demote under VMware intent only (preferred Backup proxy, not hypervisor).
_PEER_DEMOTE_DELTA = 0.10


def _role_blob(result: Any) -> str:
    """Lowercase source/target text used for preferred/demoted role matching."""
    parts: list[str] = []
    for attr in ("sourceService", "targetService"):
        val = getattr(result, attr, None)
        if val:
            parts.append(str(val))
    for meta_attr in ("source_meta", "target_meta"):
        meta = getattr(result, meta_attr, None)
        if meta is not None:
            for field in ("canonical", "original"):
                val = getattr(meta, field, None)
                if val:
                    parts.append(str(val))
            roles = getattr(meta, "roles", None) or []
            parts.extend(str(r) for r in roles)
    return " ".join(parts).lower()


def boosted_sort_key(result: Any, intent: dict[str, Any] | None) -> float:
    """Similarity plus mild boost/demote when backup-proxy VMware intent is active.

    Under ``backup_proxy_vmware`` intent:
    - preferred Backup proxy role: +_BOOST_DELTA (unless demoted)
    - CDP / guest / SureBackup / virtual lab: -_DEMOTE_DELTA (no target boost)
    - hypervisor/management target (ESXi / vCenter / …): +_TARGET_BOOST_DELTA
      when not demoted (stronger than role so proxy→ESXi/vCenter beats peers)
    - preferred Backup proxy ↔ peer data (server/repo/storage/gateway) and
      *not* a hypervisor edge: -_PEER_DEMOTE_DELTA
    """
    base = float(getattr(result, "similarity", 0.0) or 0.0)
    if not intent or not intent.get("backup_proxy_vmware"):
        return base

    blob = _role_blob(result)
    score = base

    preferred = any(frag in blob for frag in _PREFERRED_ROLE_FRAGMENTS)
    # Avoid boosting CDP/guest rows that also mention "backup" elsewhere.
    demoted = any(frag in blob for frag in _DEMOTED_ROLE_FRAGMENTS)
    hypervisor = any(frag in blob for frag in _HYPERVISOR_TARGET_FRAGMENTS)
    peer_data = any(frag in blob for frag in _PEER_DATA_FRAGMENTS)

    if demoted:
        score -= _DEMOTE_DELTA
        return score

    if preferred:
        score += _BOOST_DELTA
        if hypervisor:
            score += _TARGET_BOOST_DELTA
        elif peer_data:
            score -= _PEER_DEMOTE_DELTA
    elif hypervisor:
        # Either end looks like a hypervisor/management target.
        score += _TARGET_BOOST_DELTA
    return score


def apply_candidate_boost(
    results: Sequence[Any],
    intent: dict[str, Any] | None,
    limit: int,
) -> list[Any]:
    """Re-sort candidates by boosted score and truncate to ``limit``."""
    if not results:
        return []
    ordered = sorted(
        results,
        key=lambda r: boosted_sort_key(r, intent),
        reverse=True,
    )
    return list(ordered[:limit])


def overfetch_limit(limit: int, multiplier: int = 3) -> int:
    """Over-fetch factor for boost/dedup room (at least ``limit``)."""
    return max(int(limit) * int(multiplier), int(limit))
