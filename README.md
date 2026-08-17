# Linkspector

Automated broken link & image scanner for web creators. Point it at a URL, and a headless
Chromium crawler walks the site and reports every broken link, unreachable image, and missing
`alt` attribute in a live dashboard you can export to CSV.

- **Backend** — Python 3.11+, FastAPI, async Playwright, SQLModel on SQLite
- **Frontend** — Vite + TypeScript, Tailwind CSS (Play CDN)
- **Execution** — FastAPI `BackgroundTasks` running an asyncio crawl with bounded concurrency

## Features

- URL submission with client-side and server-side validation
- Scan jobs tracked through `pending → running → completed / failed`
- Crawl limits: depth (default 2) and page count (default 50), both configurable per scan
- Detects 4xx/5xx responses, timeouts, transport errors, and redirects that end in an error
- Flags images with a broken `src`, a missing `alt` attribute, or an empty `alt` attribute
- Live-polling dashboard with metric cards, filterable table, and CSV export

## Project layout

```
backend/
  main.py       FastAPI entry point & REST API routes
  crawler.py    Async Playwright crawler implementation
  models.py     SQLModel schemas (Scan, ScanItem)
  database.py   SQLite engine & session helpers
frontend/
  index.html    Dashboard layout
  src/main.ts   TypeScript client logic & API handlers
  src/style.css Custom styles on top of Tailwind
  package.json  Vite + TypeScript dependencies
  tsconfig.json
requirements.txt
```

## Setup

### 1. Backend

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Download the headless browser Playwright drives (one-time, ~150 MB)
playwright install chromium
```

Run the API:

```bash
uvicorn backend.main:app --reload --port 8000
```

The API is now on http://127.0.0.1:8000 with interactive docs at http://127.0.0.1:8000/docs.
SQLite lives at `backend/linkspector.db` and is created on first launch.

### 2. Frontend

Requires Node.js 18+ (`brew install node` on macOS).

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The Vite dev server proxies `/api` to `http://127.0.0.1:8000`, so
both servers need to be running.

### 3. Production build (optional)

```bash
cd frontend && npm run build
```

`backend/main.py` serves `frontend/dist/` automatically when it exists, so after a build the
whole app is available from `uvicorn backend.main:app --port 8000` at http://127.0.0.1:8000.

## API

| Method   | Endpoint                  | Description                                                  |
| -------- | ------------------------- | ------------------------------------------------------------ |
| `POST`   | `/api/scans`              | Start a scan. Body: `{ "url", "max_depth"?, "max_pages"? }`   |
| `GET`    | `/api/scans`              | List recent scans (`?limit=20`)                               |
| `GET`    | `/api/scans/{id}`         | Scan status + findings (`?issue_type=&status_code=&q=&limit=`)|
| `GET`    | `/api/scans/{id}/export`  | CSV audit report                                              |
| `DELETE` | `/api/scans/{id}`         | Delete a finished scan                                        |

Quick check from the terminal:

```bash
curl -X POST http://127.0.0.1:8000/api/scans \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com","max_depth":1,"max_pages":10}'

curl http://127.0.0.1:8000/api/scans/1
curl -o report.csv http://127.0.0.1:8000/api/scans/1/export
```

## Configuration

| Variable          | Default                 | Purpose                     |
| ----------------- | ----------------------- | --------------------------- |
| `LINKSPECTOR_DB`  | `backend/linkspector.db`| SQLite database path        |
| `VITE_API_BASE`   | `""` (Vite proxy)       | Absolute API base for the UI|

Crawl tuning (timeouts, concurrency) lives in `CrawlConfig` in `backend/crawler.py`.

## Notes & limits

- Only same-site pages are crawled; external links are status-checked but never followed.
- Images, fonts, and media are blocked during page rendering for speed and validated with
  direct HTTP requests instead.
- `robots.txt` is not consulted — scan sites you own or have permission to audit.
- Scans run in the API process, so a restart abandons in-flight jobs.
