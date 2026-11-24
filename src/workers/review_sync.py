import asyncio
import logging

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.clients.reviews import ReviewsClient
from src.clients.reviews.exceptions import ReviewsClientError
from src.config import Settings
from src.db import SessionFactory
from src.exceptions.exceptions import ReviewsUnavailableException
from src.logging_config import configure_logging
from src.repositories import ReviewSyncRepository
from src.schemas.review import ReviewNestedCreate

logger = logging.getLogger(__name__)


def _schedule_retry_or_fail(
    repo: ReviewSyncRepository,
    job,
    settings: Settings,
    last_error: str,
) -> None:
    will_retry = repo.mark_retry(
        job,
        settings.review_sync_retry_delay_seconds,
        settings.review_sync_max_attempts,
        last_error=last_error,
    )
    if will_retry:
        logger.warning(
            f"Retry review sync job {job.id} doctor_id={job.doctor_id} "
            f"attempts={job.attempts} delay={settings.review_sync_retry_delay_seconds}s"
        )
    else:
        logger.error(
            f"Review sync job failed permanently id={job.id} "
            f"doctor_id={job.doctor_id} attempts={job.attempts}"
        )


async def process_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    reviews_client: ReviewsClient,
    settings: Settings,
) -> None:
    async with session_factory() as session:
        repo = ReviewSyncRepository(session)
        jobs = await repo.claim_pending(
            settings.review_sync_batch_size,
            settings.review_sync_lock_timeout_seconds,
        )
        await session.commit()
        claimed = [
            (
                job.id,
                job.doctor_id,
                job.author_name,
                job.rating,
                job.text,
                job.idempotency_key,
            )
            for job in jobs
        ]

    if not claimed:
        return

    for job_id, doctor_id, author_name, rating, text, idempotency_key in claimed:
        review = ReviewNestedCreate(
            author_name=author_name,
            rating=rating,
            text=text,
        )
        try:
            await reviews_client.create_review(
                doctor_id,
                review,
                idempotency_key=idempotency_key,
            )
            async with session_factory() as session:
                repo = ReviewSyncRepository(session)
                job = await repo.get_by_id(job_id)
                if job is None:
                    continue
                repo.mark_done(job)
                await session.commit()
                logger.info(f"Review sync done job={job_id} doctor_id={doctor_id}")
        except ReviewsClientError as exc:
            logger.error(f"Permanent reviews error for sync job {job_id}: {exc}")
            try:
                async with session_factory() as session:
                    repo = ReviewSyncRepository(session)
                    job = await repo.get_by_id(job_id)
                    if job is None:
                        continue
                    repo.mark_failed(job, last_error=str(exc))
                    await session.commit()
            except Exception:
                logger.exception(f"Failed to mark review sync job {job_id} as failed")
        except (ReviewsUnavailableException, Exception) as exc:
            logger.exception(f"Failed to process review sync job {job_id}")
            try:
                async with session_factory() as session:
                    repo = ReviewSyncRepository(session)
                    job = await repo.get_by_id(job_id)
                    if job is None:
                        continue
                    _schedule_retry_or_fail(repo, job, settings, str(exc))
                    await session.commit()
            except Exception:
                logger.exception(f"Failed to mark retry for review sync job {job_id}")


async def run_review_sync_worker(
    session_factory: async_sessionmaker[AsyncSession],
    reviews_client: ReviewsClient,
    settings: Settings,
) -> None:
    try:
        while True:
            try:
                await process_jobs(session_factory, reviews_client, settings)
            except Exception:
                logger.exception("Review sync worker tick failed")
            await asyncio.sleep(settings.review_sync_poll_interval_seconds)
    except asyncio.CancelledError:
        logger.info("Review sync worker stopped")
        raise


async def main() -> None:
    configure_logging()
    settings = Settings()
    http_client = httpx.AsyncClient(
        base_url=settings.reviews_base_url.rstrip("/"),
        timeout=settings.reviews_timeout_seconds,
    )
    reviews_client = ReviewsClient(
        settings.reviews_base_url,
        timeout_seconds=settings.reviews_timeout_seconds,
        max_attempts=settings.reviews_max_attempts,
        backoff_base_seconds=settings.reviews_backoff_base_seconds,
        backoff_jitter_seconds=settings.reviews_backoff_jitter_seconds,
        backoff_exp_base=settings.reviews_backoff_exp_base,
        client=http_client,
    )
    logger.info("Review sync worker started")
    try:
        await run_review_sync_worker(SessionFactory, reviews_client, settings)
    finally:
        await reviews_client.aclose()
        logger.info("Review sync worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
