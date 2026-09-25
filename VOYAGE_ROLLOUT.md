# Rollout Guide: Voyage Embeddings (v1.1.0) — Plan C

Replace local fastembed/MiniLM with Voyage AI HTTP embeddings so the small VPS
never runs an ONNX model. Index builds and query embeds use the same model+dims
(`voyage-4-lite`, 512). Keyword fallback remains when Voyage or vectors fail.

**MCP contract is unchanged** (`SemanticSearchResponse` with `fallback` flag).

## Locked choices

| Setting | Value |
|---------|-------|
| Model | `voyage-4-lite` |
| Dimensions | `512` |
| Index build `input_type` | `document` |
| Query `input_type` | `query` |
| API | `POST https://api.voyageai.com/v1/embeddings` |
| Env | `VOYAGE_API_KEY` (required for vector path); optional `VOYAGE_MODEL`, `VOYAGE_DIMENSIONS` |

## Prerequisites

- `kubectl` configured for the k3s cluster
- Docker with `buildx` for linux/amd64 (if building from Apple Silicon)
- Existing `veeam-ports-enrichment` secret with `ANTHROPIC_API_KEY` (for scrape/enrich)
- A Voyage AI API key

## Rollout steps

### 1. Create a Voyage API key

Create a key in the Voyage AI dashboard. Do not commit it.

### 2. Create the Kubernetes secret

Prefer a **new** secret so scrape/enrich stays separate from query embeds:

```bash
kubectl create secret generic veeam-ports-voyage \
  --from-literal=VOYAGE_API_KEY='pa-...' \
  -n veeam-ports
```

Template (do not apply with REPLACE_ME): `k3s/voyage-secret.yaml`.

### 3. Build and push image 1.1.0

```bash
docker buildx build --platform linux/amd64 \
  -t txtxx56/ports_server:1.1.0 --push .
```

The image no longer pre-downloads MiniLM; embeddings are HTTP-only via Voyage.

### 4. Apply manifests

```bash
kubectl apply -f k3s/ports-backend-deployment.yaml
kubectl apply -f k3s/scraper-cronjob.yaml
# Optional template only — prefer kubectl create secret as in step 2:
# kubectl apply -f k3s/voyage-secret.yaml
```

Backend gets `VOYAGE_API_KEY` via `envFrom` → `veeam-ports-voyage`.
Scraper gets both `veeam-ports-enrichment` and `veeam-ports-voyage`, and runs
**without** `--skip-vectors` so weekly scrape builds Voyage embeddings (API-only, light CPU).

### 5. Trigger a scrape job (rebuild embeddings via Voyage)

```bash
kubectl create job --from=cronjob/ports-scraper manual-scrape-voyage-v1
kubectl logs -f job/manual-scrape-voyage-v1
```

Expect: scrape → enrich (Anthropic) → graph → Voyage embeddings. On success the
DB is swapped atomically; backend picks it up without restart (NullPool).

### 6. Verify semantic search

```bash
curl -s -X POST https://magicports.veeambp.com/ports_server/semantic-search \
  -H 'Content-Type: application/json' \
  -d '{"query":"ports from backup server to ESXi","limit":5}'
```

Expect `fallback: false` and non-zero `similarity` scores. If Voyage is down or
the key is missing, the API still returns results with `fallback: true` (keyword).

Eval queries live in `eval/semantic_queries.json`.

### 7. Optional: load DB from GitHub Actions artifact

If scrape should not run on the cluster, use the **Build embeddings DB** workflow
(`.github/workflows/build-embeddings-db.yml`):

1. Ensure repo secrets `VOYAGE_API_KEY` and `ANTHROPIC_API_KEY` are set.
2. Run **Actions → Build embeddings DB → Run workflow** (or wait for Monday 07:00 UTC).
3. Download the `allports-updated-db` artifact (`allports_updated.db`).
4. Copy onto the PVC (example):

```bash
# From a machine with kubectl + the downloaded DB:
kubectl cp ./allports_updated.db \
  deploy/ports-backend-deployment:/data/allports_updated.db.new
# Then atomically replace inside the pod (or use your usual swap script):
kubectl exec deploy/ports-backend-deployment -- \
  sh -c 'mv /data/allports_updated.db.new /data/allports_updated.db'
```

(Adjust path/pod name to match your cluster; NullPool picks up the new file.)

## Rollback

```bash
kubectl set image deployment/ports-backend-deployment \
  ports-backend=txtxx56/ports_server:1.0
```

Note: a DB built with Voyage 512-d vectors is incompatible with MiniLM 384-d.
If rolling back the image to fastembed, also restore a MiniLM-era DB or rebuild
with `--skip-vectors` / keyword-only until you re-embed.

## Notes

- **PVC**: embeddings at 512-d remain small relative to a 500Mi claim.
- **Scraper CronJob**: Monday 06:00 UTC; GHA off-box build Monday 07:00 UTC.
- **Secrets**: `veeam-ports-enrichment` (Anthropic) and `veeam-ports-voyage` (Voyage) are separate.
- **MCP**: no MCP repo changes; response shape unchanged.
