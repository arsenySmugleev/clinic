from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from src.exceptions.exceptions import NotFoundException
from src.models import (
    CacheInvalidationJob,
    CacheInvalidationStatus,
    ReviewSyncJob,
    ReviewSyncStatus,
)
from src.schemas.doctor import DoctorWithAppointmentCreate, DoctorWithAppointmentUpdate
from src.schemas.appointment import AppointmentNestedCreate, AppointmentUpsert
from src.schemas.review import ReviewNestedCreate
from src.services.doctor_appointment import DoctorAppointmentService
from src.uow import UnitOfWork
from tests.conftest import make_permanent_error_reviews_client, make_unavailable_reviews_client


def _create_payload(name: str = "House") -> DoctorWithAppointmentCreate:
    return DoctorWithAppointmentCreate(
        name=name,
        specialty="diagnostics",
        appointment=[
            AppointmentNestedCreate(
                time_start=datetime(2026, 8, 10, 10, 0, tzinfo=timezone.utc),
                name="checkup",
            )
        ],
        review=ReviewNestedCreate(
            author_name="patient",
            rating=5,
            text="great doctor",
        ),
    )


async def _pending_cache_keys(session) -> set[str]:
    result = await session.execute(
        select(CacheInvalidationJob.cache_key).where(
            CacheInvalidationJob.status == CacheInvalidationStatus.PENDING,
        )
    )
    return set(result.scalars().all())


async def _review_sync_jobs(session) -> list[ReviewSyncJob]:
    result = await session.execute(select(ReviewSyncJob))
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_create_and_get_doctor(doctor_service: DoctorAppointmentService):
    created = await doctor_service.create_doctor_with_appointment(_create_payload())

    assert created.name == "House"
    assert created.specialty == "diagnostics"
    assert len(created.appointment) == 1
    assert created.appointment[0].name == "checkup"
    assert len(created.reviews) == 1
    assert created.reviews[0].author_name == "patient"
    assert created.reviews[0].rating == 5
    assert await _review_sync_jobs(doctor_service.session) == []

    fetched = await doctor_service.get_doctor_with_appointment(created.id)
    assert fetched.id == created.id
    assert fetched.name == created.name
    assert len(fetched.reviews) == 1
    assert fetched.reviews[0].id == created.reviews[0].id


@pytest.mark.asyncio
async def test_get_enriches_reviews_from_live_client(
    doctor_service: DoctorAppointmentService,
):
    created = await doctor_service.create_doctor_with_appointment(_create_payload("Chase"))
    fetched = await doctor_service.get_doctor_with_appointment(created.id)

    assert len(fetched.reviews) == 1
    assert fetched.reviews[0].author_name == "patient"
    assert fetched.reviews[0].text == "great doctor"


@pytest.mark.asyncio
async def test_create_schedules_pending_review_sync_on_temporary_error(
    session,
    cache,
):
    client = make_unavailable_reviews_client()
    try:
        service = DoctorAppointmentService(UnitOfWork(session), cache, client)
        created = await service.create_doctor_with_appointment(_create_payload())

        assert created.name == "House"
        assert created.reviews == []
        jobs = await _review_sync_jobs(session)
        assert len(jobs) == 1
        assert jobs[0].status == ReviewSyncStatus.PENDING
        assert jobs[0].doctor_id == created.id
        assert jobs[0].author_name == "patient"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_create_marks_failed_review_sync_on_permanent_error(
    session,
    cache,
):
    client = make_permanent_error_reviews_client()
    try:
        service = DoctorAppointmentService(UnitOfWork(session), cache, client)
        created = await service.create_doctor_with_appointment(_create_payload())

        assert created.name == "House"
        assert created.reviews == []
        jobs = await _review_sync_jobs(session)
        assert len(jobs) == 1
        assert jobs[0].status == ReviewSyncStatus.FAILED
        assert jobs[0].doctor_id == created.id
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_get_doctor_not_found(doctor_service: DoctorAppointmentService):
    with pytest.raises(NotFoundException):
        await doctor_service.get_doctor_with_appointment(uuid4())


@pytest.mark.asyncio
async def test_get_doctor_uses_cache(doctor_service: DoctorAppointmentService, cache):
    created = await doctor_service.create_doctor_with_appointment(_create_payload())
    cache_key = doctor_service._cache_key(created.id)

    assert await cache.get(cache_key) is None

    await doctor_service.get_doctor_with_appointment(created.id)
    cached = await cache.get(cache_key)
    assert cached is not None
    assert '"name":"House"' in cached or '"name": "House"' in cached

    await cache.set(cache_key, cached.replace("House", "CachedName"))
    fetched = await doctor_service.get_doctor_with_appointment(created.id)
    assert fetched.name == "CachedName"


@pytest.mark.asyncio
async def test_update_schedules_cache_invalidation(
    doctor_service: DoctorAppointmentService,
    cache,
):
    created = await doctor_service.create_doctor_with_appointment(_create_payload())
    await doctor_service.get_doctor_with_appointment(created.id)
    cache_key = doctor_service._cache_key(created.id)
    assert await cache.get(cache_key) is not None

    updated = await doctor_service.update_doctor_with_appointment(
        created.id,
        DoctorWithAppointmentUpdate(name="Wilson"),
    )
    assert updated.name == "Wilson"
    assert cache_key in await _pending_cache_keys(doctor_service.session)
    assert await cache.get(cache_key) is not None

    await cache.delete(cache_key)
    fetched = await doctor_service.get_doctor_with_appointment(created.id)
    assert fetched.name == "Wilson"
    assert "Wilson" in (await cache.get(cache_key))


@pytest.mark.asyncio
async def test_delete_soft_deletes_and_schedules_cache_invalidation(
    doctor_service: DoctorAppointmentService,
    cache,
):
    created = await doctor_service.create_doctor_with_appointment(_create_payload())
    await doctor_service.get_doctor_with_appointment(created.id)
    cache_key = doctor_service._cache_key(created.id)

    await doctor_service.delete_doctor_with_appointment(created.id)

    assert cache_key in await _pending_cache_keys(doctor_service.session)
    await cache.delete(cache_key)
    with pytest.raises(NotFoundException):
        await doctor_service.get_doctor_with_appointment(created.id)


@pytest.mark.asyncio
async def test_update_adds_appointment(doctor_service: DoctorAppointmentService):
    created = await doctor_service.create_doctor_with_appointment(_create_payload())

    updated = await doctor_service.update_doctor_with_appointment(
        created.id,
        DoctorWithAppointmentUpdate(
            appointment=[
                AppointmentUpsert(
                    time_start=datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc),
                    name="follow-up",
                )
            ]
        ),
    )

    assert len(updated.appointment) == 2
    names = {item.name for item in updated.appointment}
    assert names == {"checkup", "follow-up"}
