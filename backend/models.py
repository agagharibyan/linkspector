"""SQLModel schemas for scan jobs and the issues they uncover.

Note: this module deliberately avoids `from __future__ import annotations` — it turns
every annotation into a string and SQLModel/SQLAlchemy cannot resolve `Relationship`
targets from those.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional

from sqlmodel import Field, Relationship, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ScanStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class ResourceType(str, Enum):
    link = "link"
    image = "image"
    page = "page"


class IssueType(str, Enum):
    broken_link = "broken_link"
    broken_image = "broken_image"
    missing_alt = "missing_alt"


class AltStatus(str, Enum):
    ok = "ok"
    missing = "missing"
    empty = "empty"


class Scan(SQLModel, table=True):
    __tablename__ = "scan"

    id: Optional[int] = Field(default=None, primary_key=True)
    url: str = Field(index=True)
    status: ScanStatus = Field(default=ScanStatus.pending, index=True)

    max_depth: int = 2
    max_pages: int = 50

    pages_checked: int = 0
    links_checked: int = 0
    images_checked: int = 0

    broken_links: int = 0
    broken_images: int = 0
    missing_alt_tags: int = 0

    current_url: Optional[str] = None
    error: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    items: List["ScanItem"] = Relationship(
        back_populates="scan",
        sa_relationship_kwargs={"cascade": "all, delete-orphan"},
    )


class ScanItem(SQLModel, table=True):
    """One recorded issue: a broken link, a broken image, or a missing alt tag."""

    __tablename__ = "scan_item"

    id: Optional[int] = Field(default=None, primary_key=True)
    scan_id: int = Field(foreign_key="scan.id", index=True)

    source_url: str
    target_url: str
    resource_type: ResourceType
    issue_type: IssueType = Field(index=True)

    status_code: Optional[int] = None
    alt_status: Optional[AltStatus] = None
    # Redirect target, timeout reason, or transport error text.
    detail: Optional[str] = None

    created_at: datetime = Field(default_factory=utcnow)

    scan: Optional[Scan] = Relationship(back_populates="items")


class ScanCreate(SQLModel):
    url: str
    max_depth: int = Field(default=2, ge=0, le=4)
    max_pages: int = Field(default=50, ge=1, le=500)


class ScanSummary(SQLModel):
    id: int
    url: str
    status: ScanStatus
    max_depth: int
    max_pages: int
    pages_checked: int
    links_checked: int
    images_checked: int
    broken_links: int
    broken_images: int
    missing_alt_tags: int
    current_url: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


class ScanDetail(SQLModel):
    scan: ScanSummary
    items: List[ScanItem]
    total_items: int
