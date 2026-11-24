from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import ReviewSyncJob, ReviewSyncStatus
from src.schemas.review import ReviewNestedCreate


class ReviewSyncRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    def add_pending(
        self,
        doctor_id: UUID,
        review: ReviewNestedCreate,
        *,
        idempotency_key: str | None = None,
        last_error: str | None = None,
    ) -> ReviewSyncJob:
        job = ReviewSyncJob(
            doctor_id=doctor_id,
            author_name=review.author_name,
            rating=review.rating,
            text=review.text,
            idempotency_key=idempotency_key or str(uuid4()),
            status=ReviewSyncStatus.PENDING,
            available_at=datetime.now(timezone.utc),
            last_error=last_error,
        )
        self.session.add(job)
        return job

    def add_failed(
        self,
        doctor_id: UUID,
        review: ReviewNestedCreate,
        *,
        idempotency_key: str | None = None,
        last_error: str | None = None,
    ) -> ReviewSyncJob:
        job = ReviewSyncJob(
            doctor_id=doctor_id,
            author_name=review.author_name,
            rating=review.rating,
            text=review.text,
            idempotency_key=idempotency_key or str(uuid4()),
            status=ReviewSyncStatus.FAILED,
            last_error=last_error,
        )
        self.session.add(job)
        return job

    async def get_by_id(self, job_id: UUID) -> ReviewSyncJob | None:
        return await self.session.get(ReviewSyncJob, job_id)

    async def fetch_claimable(self, limit: int) -> list[ReviewSyncJob]:
        now = datetime.now(timezone.utc)
        result = await self.session.execute(
            select(ReviewSyncJob)
            .where(
                or_(
                    and_(
                        ReviewSyncJob.status == ReviewSyncStatus.PENDING,
                        ReviewSyncJob.available_at <= now,
                    ),
                    and_(
                        ReviewSyncJob.status == ReviewSyncStatus.PROCESSING,
                        or_(
                            ReviewSyncJob.locked_until.is_(None),
                            ReviewSyncJob.locked_until <= now,
                        ),
                    ),
                )
            )
            .order_by(ReviewSyncJob.available_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result.scalars().all())

    async def claim_pending(
        self,
        limit: int,
        lock_timeout_seconds: int,
    ) -> list[ReviewSyncJob]:
        jobs = await self.fetch_claimable(limit)
        locked_until = datetime.now(timezone.utc) + timedelta(
            seconds=lock_timeout_seconds,
        )
        for job in jobs:
            job.status = ReviewSyncStatus.PROCESSING
            job.locked_until = locked_until
        return jobs

    def mark_done(self, job: ReviewSyncJob) -> None:
        job.status = ReviewSyncStatus.DONE
        job.locked_until = None
        job.last_error = None

    def mark_failed(self, job: ReviewSyncJob, last_error: str | None = None) -> None:
        job.status = ReviewSyncStatus.FAILED
        job.locked_until = None
        if last_error is not None:
            job.last_error = last_error

    def mark_retry(
        self,
        job: ReviewSyncJob,
        delay_seconds: int,
        max_attempts: int,
        last_error: str | None = None,
    ) -> bool:
        job.attempts += 1
        job.locked_until = None
        if last_error is not None:
            job.last_error = last_error
        if job.attempts >= max_attempts:
            self.mark_failed(job, last_error=last_error)
            return False
        job.status = ReviewSyncStatus.PENDING
        job.available_at = datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)
        return True
