# Veeam Ports Server

NOTE: this is not an official Veeam tool. Errors and omissions are accepted.

This application is a FastAPI app that fronts a SQLite database which contains all the ports from Veeam products.

The application works with the frontend application hosted at https://magicports.veeambp.com/

We decided to open source both parts of this project so everyone can benefit from it and help improve it.

If you have any suggestions for improvements please open an issue.

See the frontend project here: https://github.com/shapedthought/portsApp

## Current versions

Last updated: 11-02-2026

| Product              | Version |
| -------------------- | ------- |
| VBR VMware/Hyper-V   | 13      |
| VBR VMware (v12.3)   | 12.3    |
| VBR Hyper-V          | 12.3    |
| Agent Management     | 13      |
| Explorers            | 13      |
| VCC                  | 13      |
| VONE                 | 13      |
| VSPC                 | 9.1     |
| VRO                  | 13      |
| VB365                | 8       |
| AHV                  | 9       |
| OLVM / RHV           | 7       |
| Proxmox              | 3       |
| VBAWS                | 10      |
| VBAzure              | 8.1     |
| VBGCP                | 7       |
| Agent for Windows    | 13      |
| Agent for Linux      | 13      |

## How to run

Install dependencies:

```
pip install -r requirements.txt
```

Run the server:

```
uvicorn ports_server:app --reload --port 8001
```

The Angular frontend expects the backend on port 8001.

## Scraping

Port data is scraped from the Veeam help center using `scrape_ports.py`. URLs are configured in `configuration.json`.

```
python scrape_ports.py              # scrape all products and write to DB
python scrape_ports.py --dry-run    # scrape without writing, print summary
```

The scraper is idempotent — it drops and recreates the database table on each run.

## Database

The database is `allports_updated.db` with a single `all_ports` table.

Schema:

- `sourceService` — source service/component
- `targetService` — target service/component
- `protocol` — protocol used (TCP, UDP, etc.)
- `port` — port number(s)
- `description` — description of the connection
- `subheading` — top-level section heading from the Help Documentation
- `subheadingL2` — level 2 subheading
- `subheadingL3` — level 3 subheading
- `product` — Veeam product (e.g. VBR v13, VB365, VBAWS)

All columns are TEXT. The subheading hierarchy allows grouping ports in the frontend application.

## Docker

Build for x86 from Apple Silicon:

```
docker buildx build --platform linux/amd64 -t txtxx56/ports_server:0.9 --push .
```

Run locally:

```
docker run --rm -d -p 8001:8001 txtxx56/ports_server:0.9
```

## Deployment

The application is deployed on k3s with Traefik ingress. Kubernetes manifests are in the `k3s/` directory.

The backend and a weekly scraper CronJob share a PersistentVolumeClaim for the database, so port data is automatically refreshed without rebuilding the image.

Note that this is NOT an official Veeam tool. Errors and omissions are accepted.
