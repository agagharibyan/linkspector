"""FastAPI entry point: scan submission, polling, and CSV export."""

from __future__ import annotations

import csv
import io
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlmodel import Session, col, select

from .crawler import run_scan
from .database import init_db, get_session
from .models import (
    IssueType,
    Scan,
    ScanCreate,
    ScanDetail,
    ScanItem,
    ScanStatus,
    ScanSummary,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("linkspector.api")

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Linkspector API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)


def validate_target_url(raw: str) -> str:
    """Server-side URL validation; returns a normalized absolute http(s) URL."""
    candidate = (raw or "").strip()
    if not candidate:
        raise HTTPException(status_code=422, detail="A URL is required.")
    if len(candidate) > 2000:
        raise HTTPException(status_code=422, detail="URL is too long.")

    if "://" not in candidate:
        candidate = f"https://{candidate}"

    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="Only http:// and https:// URLs are supported.")
    if not parsed.hostname or "." not in parsed.hostname:
        if parsed.hostname not in ("localhost",):
            raise HTTPException(status_code=422, detail="Enter a valid hostname, e.g. example.com.")

    return urlunparse(parsed._replace(path=parsed.path or "/", fragment=""))


def to_summary(scan: Scan) -> ScanSummary:
    return ScanSummary.model_validate(scan, from_attributes=True)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/scans", response_model=ScanSummary, status_code=201)
def create_scan(
    payload: ScanCreate,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
) -> ScanSummary:
    url = validate_target_url(payload.url)

    scan = Scan(url=url, max_depth=payload.max_depth, max_pages=payload.max_pages)
    session.add(scan)
    session.commit()
    session.refresh(scan)

    assert scan.id is not None
    background_tasks.add_task(run_scan, scan.id)
    logger.info("Queued scan %s for %s", scan.id, url)

    return to_summary(scan)


@app.get("/api/scans", response_model=list[ScanSummary])
def list_scans(
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
) -> list[ScanSummary]:
    scans = session.exec(select(Scan).order_by(col(Scan.id).desc()).limit(limit)).all()
    return [to_summary(scan) for scan in scans]


@app.get("/api/scans/{scan_id}", response_model=ScanDetail)
def get_scan(
    scan_id: int,
    issue_type: Optional[IssueType] = None,
    status_code: Optional[int] = None,
    q: Optional[str] = None,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
) -> ScanDetail:
    scan = session.get(Scan, scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found.")

    filters = [ScanItem.scan_id == scan_id]
    if issue_type is not None:
        filters.append(ScanItem.issue_type == issue_type)
    if status_code is not None:
        filters.append(ScanItem.status_code == status_code)
    if q:
        pattern = f"%{q.strip()}%"
        filters.append(
            col(ScanItem.source_url).like(pattern) | col(ScanItem.target_url).like(pattern)
        )

    total = session.exec(select(func.count()).select_from(ScanItem).where(*filters)).one()
    items = session.exec(
        select(ScanItem).where(*filters).order_by(col(ScanItem.id).asc()).offset(offset).limit(limit)
    ).all()

    return ScanDetail(scan=to_summary(scan), items=list(items), total_items=total)


@app.get("/api/scans/{scan_id}/export")
def export_scan(scan_id: int, session: Session = Depends(get_session)) -> StreamingResponse:
    scan = session.get(Scan, scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found.")

    items = session.exec(
        select(ScanItem).where(ScanItem.scan_id == scan_id).order_by(col(ScanItem.id).asc())
    ).all()

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "source_page",
            "target_resource",
            "resource_type",
            "issue_type",
            "status_code",
            "alt_status",
            "detail",
            "detected_at",
        ]
    )
    for item in items:
        writer.writerow(
            [
                item.source_url,
                item.target_url,
                item.resource_type.value,
                item.issue_type.value,
                item.status_code if item.status_code is not None else "",
                item.alt_status.value if item.alt_status else "",
                item.detail or "",
                item.created_at.isoformat(),
            ]
        )

    host = urlparse(scan.url).hostname or "site"
    filename = f"linkspector-{host}-scan-{scan_id}.csv"
    buffer.seek(0)

    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/scans/{scan_id}")
def delete_scan(scan_id: int, session: Session = Depends(get_session)) -> Response:
    scan = session.get(Scan, scan_id)
    if scan is None:
        raise HTTPException(status_code=404, detail="Scan not found.")
    if scan.status is ScanStatus.running:
        raise HTTPException(status_code=409, detail="Cannot delete a running scan.")
    session.delete(scan)
    session.commit()
    return Response(status_code=204)


# Serve the built dashboard when `npm run build` has been run.
if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")
