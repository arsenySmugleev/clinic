from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.cache import RedisCache, get_cache
from infrastructure.reviews import ReviewsClient
from src.db import get_session
from src.services.doctor_appointment import DoctorAppointmentService
from src.services.med_card_insurance import MedCardInsuranceService
from src.services.patient_med_service import PatientMedServiceService


def get_reviews_client(request: Request) -> ReviewsClient:
    return request.app.state.reviews_client


def get_doctor_appointment_service(
    session: AsyncSession = Depends(get_session),
    cache: RedisCache = Depends(get_cache),
    reviews: ReviewsClient = Depends(get_reviews_client),
) -> DoctorAppointmentService:
    return DoctorAppointmentService(session, cache, reviews)


def get_patient_med_service_service(
    session: AsyncSession = Depends(get_session),
    cache: RedisCache = Depends(get_cache),
) -> PatientMedServiceService:
    return PatientMedServiceService(session, cache)


def get_med_card_insurance_service(
    session: AsyncSession = Depends(get_session),
    cache: RedisCache = Depends(get_cache),
) -> MedCardInsuranceService:
    return MedCardInsuranceService(session, cache)
