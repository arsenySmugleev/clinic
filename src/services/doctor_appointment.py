import logging
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from infrastructure.cache import RedisCache
from src.clients.reviews import ReviewsClient
from src.clients.reviews.exceptions import ReviewsClientError
from src.exceptions.exceptions import NotFoundException, ReviewsUnavailableException
from src.models.appointment import Appointment as AppointmentModel
from src.models.doctor import Doctor as DoctorModel
from src.repositories import CacheInvalidationRepository, ReviewSyncRepository
from src.schemas.doctor import (
    DoctorWithAppointmentCreate,
    DoctorWithAppointmentResponse,
    DoctorWithAppointmentUpdate,
)
from src.uow import UnitOfWork

logger = logging.getLogger(__name__)


class DoctorAppointmentService:

    def __init__(
        self,
        uow: UnitOfWork,
        cache: RedisCache,
        reviews_client: ReviewsClient,
    ):
        self.uow = uow
        self.session = uow.session
        self.cache = cache
        self.reviews_client = reviews_client
        self.invalidation_repo = CacheInvalidationRepository(self.session)
        self.review_sync_repo = ReviewSyncRepository(self.session)

    @staticmethod
    def _cache_key(doctor_id: UUID) -> str:
        return f"doctor:{doctor_id}"

    def _schedule_cache_invalidation(self, *cache_keys: str) -> None:
        for cache_key in cache_keys:
            self.invalidation_repo.add(cache_key)

    async def _get_doctor_model(self, doctor_id: UUID) -> DoctorModel:
        result = await self.session.execute(
            select(DoctorModel)
            .where(
                DoctorModel.id == doctor_id,
                DoctorModel.is_deleted.is_(False),
            )
            .options(
                selectinload(
                    DoctorModel.appointment.and_(AppointmentModel.is_deleted.is_(False))
                )
            )
            .execution_options(populate_existing=True)
        )
        doctor = result.scalar_one_or_none()
        if not doctor:
            message = f"Doctor {doctor_id} not found"
            logger.warning(message)
            raise NotFoundException(message)
        return doctor

    async def get_doctor_with_appointment(self, doctor_id: UUID) -> DoctorWithAppointmentResponse:
        cache_key = self._cache_key(doctor_id)
        cached = await self.cache.get(cache_key)
        if cached:
            response = DoctorWithAppointmentResponse.model_validate_json(cached)
            return await response.with_reviews(self.reviews_client)

        doctor = await self._get_doctor_model(doctor_id)
        response = DoctorWithAppointmentResponse.from_model(doctor)
        await self.cache.set(
            cache_key,
            response.model_dump_json(exclude={"reviews"}),
        )
        return await response.with_reviews(self.reviews_client)

    async def create_doctor_with_appointment(
        self,
        doctor_data: DoctorWithAppointmentCreate,
    ) -> DoctorWithAppointmentResponse:
        doctor = doctor_data.map_data()

        self.session.add(doctor)
        await self.session.flush()
        doctor = await self._get_doctor_model(doctor.id)
        response = DoctorWithAppointmentResponse.from_model(doctor)
        await self.uow.commit()

        idempotency_key = str(uuid4())
        try:
            created = await self.reviews_client.create_review(
                doctor.id,
                doctor_data.review,
                idempotency_key=idempotency_key,
            )
        except ReviewsUnavailableException as exc:
            logger.warning(
                f"Reviews unavailable after create doctor {doctor.id}, scheduling sync: {exc}"
            )
            self.review_sync_repo.add_pending(
                doctor.id,
                doctor_data.review,
                idempotency_key=idempotency_key,
                last_error=str(exc),
            )
            await self.uow.commit()
            return response
        except ReviewsClientError as exc:
            logger.error(
                f"Permanent reviews error after create doctor {doctor.id}: {exc}"
            )
            self.review_sync_repo.add_failed(
                doctor.id,
                doctor_data.review,
                idempotency_key=idempotency_key,
                last_error=str(exc),
            )
            await self.uow.commit()
            return response

        return response.model_copy(update={"reviews": [created]})

    async def update_doctor_with_appointment(
        self,
        doctor_id: UUID,
        update_data: DoctorWithAppointmentUpdate,
    ) -> DoctorWithAppointmentResponse:
        doctor = await self._get_doctor_model(doctor_id)
        new_appointments = update_data.apply_to(doctor)
        if new_appointments:
            self.session.add_all(new_appointments)
        await self.session.flush()
        doctor = await self._get_doctor_model(doctor_id)
        response = DoctorWithAppointmentResponse.from_model(doctor)
        self._schedule_cache_invalidation(self._cache_key(doctor_id))
        await self.uow.commit()
        return await response.with_reviews(self.reviews_client)

    async def delete_doctor_with_appointment(self, doctor_id: UUID) -> None:
        doctor = await self._get_doctor_model(doctor_id)
        doctor.is_deleted = True
        for appointment in doctor.appointment:
            appointment.is_deleted = True
        await self.session.flush()
        self._schedule_cache_invalidation(self._cache_key(doctor_id))
        await self.uow.commit()
