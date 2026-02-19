# Rollout Guide: API Enhancements (v1.0)

Deployment steps for the `feature/api-enhancements` branch changes.
PR: https://github.com/shapedthought/ports_server/pull/30

## What changed

- Subsection/port exclusion filtering on topology and app-import endpoints
- Service discovery endpoint (`GET /products/{name}/services`)
- Fuzzy-match warnings for unresolved service names
- CSV and markdown output formats
- ESXi synonym merge at graph build time and runtime
- App-import now populates inbound fields, normalizes port strings, writes a top-level array

## Prerequisites

- `kubectl` configured for the k3s cluster
- Docker with `buildx` for cross-compilation (if building from Apple Silicon)
- The `veeam-ports-enrichment` secret already exists with a valid `ANTHROPIC_API_KEY`

## Rollout order

### 1. Merge the PR

```bash
gh pr merge 30 --squash
git checkout master && git pull
```

### 2. Build and push the Docker image

From the repo root on Apple Silicon:

```bash
docker buildx build --platform linux/amd64 \
  -t txtxx56/ports_server:1.0 --push .
```

This image includes all dependencies (`fastembed`, `sqlite-vec`, `anthropic`) and pre-downloads the embedding model.

### 3. Apply updated k3s manifests

The backend deployment and scraper cronjob manifests have been updated to use `txtxx56/ports_server:1.0`.

```bash
# Update backend deployment (triggers rolling restart)
kubectl apply -f k3s/ports-backend-deployment.yaml

# Update scraper cronjob (next scheduled run uses new image)
kubectl apply -f k3s/scraper-cronjob.yaml
```

### 4. Trigger a manual scrape

The new image includes `graph_builder.py` with synonym normalization. A re-scrape is required to rebuild the knowledge graph with merged synonyms.

```bash
kubectl create job --from=cronjob/ports-scraper manual-scrape-v1
```

Monitor progress:

```bash
kubectl logs -f job/manual-scrape-v1
```

The scraper runs the full pipeline: scrape -> enrich (Anthropic API) -> build graph -> build embeddings. Expect ~5-10 minutes depending on API latency.

### 5. Verify the deployment

```bash
# Health check
curl https://magicports.veeambp.com/ports_server/health

# Service discovery
curl https://magicports.veeambp.com/ports_server/products/VBR%20v13/services | head

# App-import with CDP exclusion (should include 443 to ESXi)
curl -s -X POST https://magicports.veeambp.com/ports_server/products/VBR%20v13/app-import \
  -H 'Content-Type: application/json' \
  -d '{
    "servers": [
      {"name": "VBR", "services": ["Backup server"]},
      {"name": "ESXi", "services": ["ESXi host"]}
    ],
    "options": {"exclude_subsections": ["CDP Components"]}
  }'
```

### 6. Verify the scrape completed

```bash
kubectl get jobs | grep manual-scrape-v1
```

Once the scrape job completes, the backend automatically picks up the new DB (atomic swap via `os.replace`). No backend restart needed after scraping.

## Rollback

If issues arise, revert to the previous image:

```bash
kubectl set image deployment/ports-backend-deployment \
  ports-backend=txtxx56/ports_server:0.92
```

## Notes

- **PVC**: 500Mi, current DB is ~2MB (well within limits even with embeddings)
- **Scraper CronJob**: runs weekly (Monday 06:00 UTC), uses the same image as the backend
- **Anthropic secret**: `veeam-ports-enrichment` must exist with `ANTHROPIC_API_KEY` for enrichment to succeed
- **MCP server** (`veeam-ports-mcp`): changes already pushed to `main`. Claude Desktop picks up changes on restart via `uv run --directory` — no separate deployment needed
