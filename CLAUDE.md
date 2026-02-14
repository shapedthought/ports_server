# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Veeam Ports Server — a FastAPI REST API that serves network port requirements for Veeam products from a SQLite database. Deployed on k3s with Traefik ingress at `magicports.veeambp.com`. Works with an Angular frontend (source: https://github.com/shapedthought/portsApp).

## Commands

### Run locally
```bash
pip install -r requirements.txt
uvicorn ports_server:app --reload --port 8001
```

### Scrape port data
```bash
python scrape_ports.py              # full scrape → writes allports_updated.db
python scrape_ports.py --dry-run    # scrape without writing to DB
python scrape_ports.py --db other.db  # write to a specific DB file
```

The `DB_PATH` env var overrides the default DB location (used in k3s to point at the PVC mount).

### Docker (cross-compile for x86 from Apple Silicon)
```bash
docker buildx build --platform linux/amd64 -t txtxx56/ports_server:0.91 --push .
```

The app runs on port **8001**.

## Architecture

**Stack:** FastAPI, SQLAlchemy, Pydantic, SQLite, uvicorn. Pandas/openpyxl for Excel export. BeautifulSoup for scraping.

**Key files:**
- `ports_server.py` — All API routes. SQLAlchemy engine with reflected `all_ports` table. DB path configurable via `DB_PATH` env var.
- `models.py` — Pydantic models for requests and responses. All field names are camelCase matching DB columns directly.
- `scrape_ports.py` — Standalone scraper that reads `configuration.json`, scrapes Veeam help center pages, and writes to the DB. Idempotent (drops and recreates table each run).
- `configuration.json` — Array of `{url, product}` entries for each Veeam help center ports page.
- `allports_updated.db` — SQLite database with a single `all_ports` table (local dev copy).

**Database schema** (`all_ports` table): `subheading`, `subheadingL2`, `subheadingL3`, `product`, `sourceService`, `targetService`, `protocol`, `port`, `description` — all TEXT, camelCase column names.

**Scraping flow:** `configuration.json` → BeautifulSoup parses HTML for `span.Subheading`/`span.Subheading_L2`/`span.Subheading_L3` + `table` elements → DataFrame per table → clean/normalize → write to SQLite.

## k3s Deployment

Manifests are in `k3s/`. The backend and a scraper CronJob share a PVC for the database.

```
Internet → magicports.veeambp.com (TLS via Let's Encrypt + cert-manager)
  ├─ /ports_server/* → Traefik strips prefix → backend:8001 (FastAPI)
  └─ /*              → frontend:80 (Angular)
```

| Resource | Manifest | Notes |
|----------|----------|-------|
| Backend deployment | `ports-backend-deployment.yaml` | Mounts PVC at `/data`, `DB_PATH=/data/allports_updated.db` |
| Backend service | `ports-backend-service.yaml` | ClusterIP on port 8001 |
| Scraper CronJob | `scraper-cronjob.yaml` | Weekly (Mon 06:00 UTC), same image, overrides command |
| Database PVC | `ports-db-pvc.yaml` | 100Mi, shared between backend and scraper |
| Ingress | `ingress.yaml` | Traefik with TLS, path-based routing |
| Middleware | `traefik-middleware.yaml` | Strips `/ports_server` prefix |
| TLS issuer | `letsencrypt-clusterissuer.yaml` | Let's Encrypt prod via HTTP-01 |

**First deploy:** Create PVC → run initial scrape job → then deploy backend.
**Trigger manual scrape:** `kubectl create job --from=cronjob/ports-scraper manual-scrape`

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | List distinct products |
| GET | `/health` | Health check |
| POST | `/source` | Distinct source services for a product |
| POST | `/sourceDetails` | Source services with subheading grouping |
| POST | `/target` | Target services for a source/product |
| POST | `/allTarget` | All ports for source/product/subheading |
| POST | `/ports` | Ports for a specific source/target/product |
| POST | `/generateExcelWithUrl` | Generate Excel file from port data |
| GET | `/download/{filename}` | Download generated Excel file |
