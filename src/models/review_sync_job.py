from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from src.models.base import Base


class ReviewSyncStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class ReviewSyncJob(Base):
    __tablename__ = "review_sync_jobs"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    doctor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    author_name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    rating: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    text: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(
        sa.String(),
        nullable=False,
        default=ReviewSyncStatus.PENDING,
        server_default=ReviewSyncStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(
        sa.Integer(),
        nullable=False,
        default=0,
        server_default="0",
    )
    available_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True),
        nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
