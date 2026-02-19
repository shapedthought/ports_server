"""Shared synonym definitions for canonical name normalization.

Imported by both ports_server.py (runtime resolution) and
graph_builder.py (graph build-time merge).
"""

# Each set groups canonical names that refer to the same logical component.
# The alphabetically-first name in each set becomes the canonical representative.
# All values must be lowercase.
ROLE_SYNONYM_GROUPS: list[set[str]] = [
    {"esxi server", "esxi host"},
]

# Derived lookups — built once at import time.
SYNONYM_INDEX: dict[str, set[str]] = {}
SYNONYM_CANONICAL: dict[str, str] = {}

for _group in ROLE_SYNONYM_GROUPS:
    _representative = min(_group)  # alphabetically first for stability
    for _name in _group:
        SYNONYM_INDEX[_name] = _group - {_name}
        SYNONYM_CANONICAL[_name] = _representative
