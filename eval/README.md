# Search-quality eval

Cases live in `semantic_queries.json`:

- `queries` — simple NL list (Voyage / Plan C smoke queries)
- `cases` — Phase 0 regression harness (`mode`: `keyword` | `semantic`) with
  expected hits for proxy/VMware ranking and keyword token-AND

## Run locally

```bash
# Keyword cases against the checked-in all_ports DB (no Voyage needed)
python eval/run_eval.py --mode keyword

# Unit tests (rewrite / boost / keyword integration)
python -m pytest tests/test_search_quality.py -v

# Semantic cases need a running API with enriched_ports + embeddings
PORTS_BASE_URL=http://127.0.0.1:8001 python eval/run_eval.py --mode semantic
# or against prod:
PORTS_BASE_URL=https://magicports.veeambp.com/ports_server \
  python eval/run_eval.py --mode semantic
```

Product names: live data uses `VBR VMware v12.3`; older local scrapes may still
say `VBR`. Semantic cases list `product_aliases` so either works.
