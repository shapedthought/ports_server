#!/usr/bin/env python3
"""Run MagicPorts search-quality eval cases from semantic_queries.json.

Modes
-----
keyword   Local SQLite via GET /search logic (no Voyage). Default DB:
          allports_updated.db or $DB_PATH.
semantic  POST /semantic-search against a live base URL
          ($PORTS_BASE_URL, default http://127.0.0.1:8001).

Examples
--------
  # Keyword cases only (works on the checked-in all_ports DB):
  python eval/run_eval.py --mode keyword

  # Semantic cases against a running server (Voyage + enriched DB):
  PORTS_BASE_URL=https://magicports.veeambp.com/ports_server \\
    python eval/run_eval.py --mode semantic

  # Everything the harness can run:
  python eval/run_eval.py --mode all

Exit code 0 = all selected cases passed; 1 = one or more failed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CASES_PATH = Path(__file__).with_name("semantic_queries.json")


def _load_cases() -> list[dict[str, Any]]:
    data = json.loads(CASES_PATH.read_text())
    return list(data.get("cases") or [])


def _match_expect(row: dict[str, Any], expect: dict[str, str]) -> bool:
    src = str(row.get("sourceService") or "")
    tgt = str(row.get("targetService") or "")
    port = str(row.get("port") or "")
    sc = expect.get("source_contains") or ""
    tc = expect.get("target_contains") or ""
    pc = expect.get("port_contains") or ""
    if sc and sc.lower() not in src.lower():
        return False
    if tc and tc.lower() not in tgt.lower():
        return False
    if pc and pc.lower() not in port.lower():
        return False
    # At least one non-empty criterion must have been checked
    return bool(sc or tc or pc)


def _any_expect_hit(rows: list[dict[str, Any]], expects: list[dict[str, str]]) -> bool:
    for row in rows:
        for exp in expects:
            if _match_expect(row, exp):
                return True
    return False


def _run_keyword_case(case: dict[str, Any]) -> tuple[bool, str]:
    # Import after sys.path tweak; reuse the same helpers as the API.
    from sqlalchemy import create_engine, MetaData, Table, select
    from search_quality import KEYWORD_SEARCH_COLUMNS
    import ports_server as ps

    db_path = os.environ.get("DB_PATH", str(ROOT / "allports_updated.db"))
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    metadata = MetaData()
    table = Table("all_ports", metadata, autoload_with=engine)
    where = ps._token_and_where(table, KEYWORD_SEARCH_COLUMNS, case["query"])
    if where is None:
        return False, "empty query / no tokens"

    with engine.connect() as conn:
        rows = [dict(r) for r in conn.execute(select(table).where(where)).mappings()]

    min_hits = int(case.get("expect_min_hits") or 1)
    if len(rows) < min_hits:
        return False, f"hits={len(rows)} < expect_min_hits={min_hits}"

    expects = case.get("expect_any") or []
    if expects and not _any_expect_hit(rows, expects):
        sample = [
            f"{r.get('sourceService')}->{r.get('targetService')}:{r.get('port')}"
            for r in rows[:5]
        ]
        return False, f"no expected row in {len(rows)} hits; sample={sample}"

    return True, f"hits={len(rows)}"


def _pick_product(case: dict[str, Any], available: set[str] | None) -> str | None:
    primary = case.get("product")
    aliases = list(case.get("product_aliases") or [])
    candidates = []
    if primary:
        candidates.append(primary)
    candidates.extend(a for a in aliases if a not in candidates)
    if not candidates:
        return None
    if not available:
        return candidates[0]
    for c in candidates:
        if c in available:
            return c
    return candidates[0]


def _run_semantic_case(case: dict[str, Any], base_url: str) -> tuple[bool, str]:
    import urllib.error
    import urllib.request

    product = case.get("product")
    # Try aliases if the primary product 404s / returns empty — caller may pass
    # product_aliases for older local DBs (VBR vs VBR VMware v12.3).
    products_to_try = []
    if product:
        products_to_try.append(product)
    for a in case.get("product_aliases") or []:
        if a not in products_to_try:
            products_to_try.append(a)
    if not products_to_try:
        products_to_try = [None]

    limit = int(case.get("limit") or 5)
    last_detail = ""
    for prod in products_to_try:
        body = {"query": case["query"], "limit": limit}
        if prod:
            body["product"] = prod
        req = urllib.request.Request(
            base_url.rstrip("/") + "/semantic-search",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read().decode())
        except urllib.error.URLError as exc:
            return False, f"request failed: {exc}"

        results = payload.get("results") or []
        expects = case.get("expect_top_k_any") or []
        if expects and _any_expect_hit(results, expects):
            return True, f"product={prod!r} top{limit} hit (fallback={payload.get('fallback')})"
        sample = [
            f"{r.get('sourceService')}->{r.get('targetService')}:{r.get('port')}@{r.get('similarity')}"
            for r in results[:5]
        ]
        last_detail = (
            f"product={prod!r} no expected row in top{limit}; "
            f"fallback={payload.get('fallback')}; sample={sample}"
        )
        # If we got results but wrong ranking, don't bother other aliases unless empty
        if results and prod is not None:
            # try next alias only when zero results (wrong product name)
            if len(results) > 0 and prod == products_to_try[0]:
                # still try aliases in case product filter excluded everything
                continue
    return False, last_detail or "no results"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("keyword", "semantic", "all"),
        default="keyword",
        help="Which case modes to run (default: keyword)",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("PORTS_BASE_URL", "http://127.0.0.1:8001"),
        help="Base URL for semantic cases (no trailing path beyond /ports_server if used)",
    )
    args = parser.parse_args()

    cases = _load_cases()
    selected = []
    for case in cases:
        mode = case.get("mode")
        if args.mode == "all" or mode == args.mode:
            selected.append(case)

    if not selected:
        print(f"No cases for mode={args.mode}")
        return 1

    passed = failed = 0
    print(f"Running {len(selected)} case(s) [mode={args.mode}]")
    for case in selected:
        mode = case.get("mode")
        try:
            if mode == "keyword":
                ok, detail = _run_keyword_case(case)
            elif mode == "semantic":
                ok, detail = _run_semantic_case(case, args.base_url)
            else:
                ok, detail = False, f"unknown mode {mode!r}"
        except Exception as exc:  # noqa: BLE001 — report and continue
            ok, detail = False, f"exception: {exc}"

        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {case.get('id')}: {detail}")
        if ok:
            passed += 1
        else:
            failed += 1

    print(f"\nSummary: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
