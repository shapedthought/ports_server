"""Unit tests for Phase 1 keyword matching and Phase 2 rewrite/boost."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import MetaData, Table, create_engine, select

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from search_quality import (  # noqa: E402
    _OVERFETCH_CAP,
    _OVERFETCH_FLOOR,
    apply_candidate_boost,
    build_haystack,
    detect_backup_proxy_vmware_intent,
    haystack_matches_query,
    overfetch_limit,
    rewrite_proxy_query,
    synonym_query_variants,
    tokenize_query,
)


def test_tokenize_splits_words():
    assert tokenize_query("Backup proxy ESXi server") == [
        "backup",
        "proxy",
        "esxi",
        "server",
    ]


def test_synonym_variants_swap_esxi_host_server():
    variants = synonym_query_variants("Backup proxy ESXi host")
    assert "backup proxy esxi host" in variants
    assert "backup proxy esxi server" in variants


def test_haystack_token_and_across_fields():
    hay = build_haystack(
        ["Backup proxy", "ESXi server", "902", "VBR", "data transfer"]
    )
    assert haystack_matches_query(hay, "Backup proxy ESXi server")
    assert haystack_matches_query(hay, "Backup proxy ESXi host")
    assert not haystack_matches_query(hay, "CDP proxy ESXi")


def test_rewrite_bare_proxy_vmware():
    q, intent = rewrite_proxy_query("what ports does the proxy need for VMware")
    assert intent["backup_proxy_vmware"] is True
    assert "backup proxy" in q.lower()
    assert "veeam proxy" in q.lower()


def test_rewrite_skips_when_cdp_named():
    q, intent = rewrite_proxy_query("CDP proxy ports for VMware")
    assert intent["backup_proxy_vmware"] is False
    assert q == "CDP proxy ports for VMware"


def test_rewrite_skips_when_guest_named():
    q, intent = rewrite_proxy_query("guest interaction proxy VMware")
    assert intent["backup_proxy_vmware"] is False
    assert q == "guest interaction proxy VMware"


def test_rewrite_noop_without_vmware_cue():
    q, intent = rewrite_proxy_query("what ports does the proxy need")
    assert intent["backup_proxy_vmware"] is False
    assert q == "what ports does the proxy need"


def test_rewrite_keeps_existing_backup_proxy_phrasing():
    q, intent = rewrite_proxy_query("Backup proxy to ESXi for VMware")
    assert intent["backup_proxy_vmware"] is True
    assert q == "Backup proxy to ESXi for VMware"


def _result(src, tgt, sim, port="902"):
    return SimpleNamespace(
        sourceService=src,
        targetService=tgt,
        port=port,
        similarity=sim,
        source_meta=SimpleNamespace(canonical=src.lower(), original=src, roles=[]),
        target_meta=SimpleNamespace(canonical=tgt.lower(), original=tgt, roles=[]),
    )


def test_boost_prefers_backup_proxy_over_cdp():
    intent = {"backup_proxy_vmware": True}
    results = [
        _result("CDP proxy (source)", "ESXi host (source)", 0.68, "902"),
        _result("Guest interaction proxy", "ESXi server", 0.65, "443"),
        _result("Backup proxy", "ESXi server", 0.55, "902"),
        _result("Backup proxy", "vCenter Server", 0.54, "443"),
    ]
    top = apply_candidate_boost(results, intent, limit=2)
    assert top[0].sourceService == "Backup proxy"
    assert top[1].sourceService == "Backup proxy"
    assert {r.port for r in top} == {"902", "443"}


def test_boost_noop_without_intent():
    intent = {"backup_proxy_vmware": False}
    results = [
        _result("CDP proxy (source)", "ESXi host", 0.9),
        _result("Backup proxy", "ESXi server", 0.5),
    ]
    top = apply_candidate_boost(results, intent, limit=1)
    assert top[0].sourceService.startswith("CDP")


@pytest.mark.parametrize(
    "query",
    [
        "Backup proxy ESXi server",
        "Backup proxy ESXi host",
    ],
)
def test_keyword_search_hits_backup_proxy_esxi(query):
    """Integration against the checked-in all_ports SQLite DB."""
    import ports_server as ps
    from search_quality import KEYWORD_SEARCH_COLUMNS

    db_path = os.path.join(ROOT, "allports_updated.db")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    table = Table("all_ports", MetaData(), autoload_with=engine)
    where = ps._token_and_where(table, KEYWORD_SEARCH_COLUMNS, query)
    assert where is not None

    with engine.connect() as conn:
        rows = list(conn.execute(select(table).where(where)).mappings())

    assert rows, f"expected hits for {query!r}"
    assert any(
        "backup proxy" in (r["sourceService"] or "").lower()
        and "esxi" in (r["targetService"] or "").lower()
        and "902" in (r["port"] or "")
        for r in rows
    )


def test_keyword_single_token_still_works():
    import ports_server as ps
    from search_quality import KEYWORD_SEARCH_COLUMNS

    db_path = os.path.join(ROOT, "allports_updated.db")
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    table = Table("all_ports", MetaData(), autoload_with=engine)
    where = ps._token_and_where(table, KEYWORD_SEARCH_COLUMNS, "902")
    with engine.connect() as conn:
        rows = list(conn.execute(select(table).where(where)).mappings())
    assert len(rows) >= 1
    assert any("902" in (r["port"] or "") for r in rows)


def test_vmware_intent_boosts_hypervisor_targets_over_peer_data():
    """Live failure shape: peer data ports outrank Backup proxy→ESXi/vCenter.

    With backup_proxy_vmware intent, target boost (+ peer demote) must lift
    ESXi 902 / vCenter 443 into top 5 (ideally top 2). CDP stays demoted.
    """
    intent = {"backup_proxy_vmware": True}
    results = [
        _result("Backup proxy", "Backup server", 0.90, "2500-3300"),
        _result("Backup proxy", "Object storage", 0.88, "443"),
        _result("CDP proxy", "ESXi server", 0.85, "902"),
        _result("Backup proxy", "ESXi server", 0.70, "902"),
        _result("Backup proxy", "vCenter Server", 0.68, "443"),
        _result("Backup proxy", "Backup repository", 0.87, "2500"),
    ]
    top = apply_candidate_boost(results, intent, limit=5)
    labels = [(r.sourceService, r.targetService, r.port) for r in top]
    # ESXi and vCenter must appear in top 5
    assert any(t == "ESXi server" and p == "902" for _, t, p in labels), labels
    assert any(t == "vCenter Server" and p == "443" for _, t, p in labels), labels
    # Ideally top 2 are the hypervisor edges
    assert top[0].targetService in ("ESXi server", "vCenter Server")
    assert top[1].targetService in ("ESXi server", "vCenter Server")
    assert {top[0].targetService, top[1].targetService} == {
        "ESXi server",
        "vCenter Server",
    }
    # CDP still demoted out of top (or at least below Backup proxy→ESXi)
    assert not any(r.sourceService.startswith("CDP") for r in top[:2])


def test_vmware_intent_off_keeps_raw_similarity_order():
    """Without intent, peer data stays above lower-sim ESXi/vCenter."""
    intent = {"backup_proxy_vmware": False}
    results = [
        _result("Backup proxy", "Backup server", 0.90, "2500-3300"),
        _result("Backup proxy", "Object storage", 0.88, "443"),
        _result("CDP proxy", "ESXi server", 0.85, "902"),
        _result("Backup proxy", "ESXi server", 0.70, "902"),
        _result("Backup proxy", "vCenter Server", 0.68, "443"),
    ]
    top = apply_candidate_boost(results, intent, limit=5)
    assert top[0].targetService == "Backup server"
    assert top[1].targetService == "Object storage"
    assert top[2].sourceService.startswith("CDP")
    # ESXi stays lower by raw similarity
    esxi_idx = next(i for i, r in enumerate(top) if r.targetService == "ESXi server" and r.sourceService == "Backup proxy")
    assert esxi_idx >= 3


def test_vmware_intent_still_demotes_cdp_and_guest():
    """CDP/guest remain demoted when VMware intent is on (even with ESXi target)."""
    intent = {"backup_proxy_vmware": True}
    results = [
        _result("CDP proxy", "ESXi server", 0.85, "902"),
        _result("Guest interaction proxy", "vCenter Server", 0.80, "443"),
        _result("Backup proxy", "ESXi server", 0.70, "902"),
        _result("Backup proxy", "vCenter Server", 0.68, "443"),
    ]
    top = apply_candidate_boost(results, intent, limit=2)
    assert all(r.sourceService == "Backup proxy" for r in top)
    assert {r.port for r in top} == {"902", "443"}



def test_detect_intent_helpers():
    assert detect_backup_proxy_vmware_intent("proxy for VMware")
    assert not detect_backup_proxy_vmware_intent("SureBackup proxy VMware")

def test_overfetch_limit_floor_for_small_limit():
    """limit=5 with default multiplier 3 would be 15; floor raises to 50."""
    assert overfetch_limit(5) == _OVERFETCH_FLOOR
    assert overfetch_limit(5) >= 50


def test_overfetch_limit_multiplier_when_above_floor():
    assert overfetch_limit(20) == max(20 * 3, _OVERFETCH_FLOOR)  # 60


def test_overfetch_limit_floor_for_limit_one():
    assert overfetch_limit(1) == _OVERFETCH_FLOOR


def test_overfetch_limit_never_less_than_limit():
    assert overfetch_limit(1) >= 1
    assert overfetch_limit(5) >= 5
    assert overfetch_limit(20) >= 20
    assert overfetch_limit(100) >= 100
    # Cap must not shrink below the requested limit
    assert overfetch_limit(250) >= 250


def test_overfetch_limit_respects_cap():
    # 80 * 3 = 240 would exceed cap; clamp to cap when limit <= cap
    assert overfetch_limit(80) == _OVERFETCH_CAP

