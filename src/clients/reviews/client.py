import logging
from http import HTTPStatus
from uuid import UUID, uuid4

import httpx
from tenacity import (
    AsyncRetrying,
    before_sleep_log,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from src.clients.reviews.exceptions import ReviewsClientError, ReviewsRetryableError
from src.clients.reviews.mapping import map_create_payload, map_review, map_reviews
from src.exceptions.exceptions import ReviewsUnavailableException
from src.schemas.review import ReviewNestedCreate, ReviewResponse

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS_CODES = frozenset({
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
})


class ReviewsClient:

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        max_attempts: int,
        backoff_base_seconds: float,
        backoff_jitter_seconds: float,
        backoff_exp_base: float,
        client: httpx.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_jitter = backoff_jitter_seconds
        self._backoff_exp_base = backoff_exp_base
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_by_doctor(self, doctor_id: UUID) -> list[ReviewResponse]:
        response = await self._request(
            "GET",
            f"/api/v1/doctors/{doctor_id}/reviews",
        )
        return map_reviews(response.json())

    async def create_review(
        self,
        doctor_id: UUID,
        review: ReviewNestedCreate,
        *,
        idempotency_key: str | None = None,
    ) -> ReviewResponse:
        payload = map_create_payload(doctor_id, review)
        response = await self._request(
            "POST",
            "/api/v1/reviews",
            json=payload,
            headers={"Idempotency-Key": idempotency_key or str(uuid4())},
        )
        return map_review(response.json())

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=wait_exponential_jitter(
                    initial=self._backoff_base,
                    jitter=self._backoff_jitter,
                    exp_base=self._backoff_exp_base,
                ),
                retry=retry_if_exception_type(ReviewsRetryableError),
                reraise=True,
                before_sleep=before_sleep_log(logger, logging.WARNING),
            ):
                with attempt:
                    try:
                        response = await self._client.request(
                            method,
                            path,
                            json=json,
                            headers=headers,
                        )
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        raise ReviewsRetryableError(str(exc)) from exc

                    status_code = response.status_code
                    if status_code in _RETRYABLE_STATUS_CODES:
                        raise ReviewsRetryableError(
                            f"retryable status {status_code}",
                            status_code=status_code,
                        )
                    if status_code >= HTTPStatus.BAD_REQUEST:
                        raise ReviewsClientError(
                            f"reviews request failed with status {status_code}",
                            status_code=status_code,
                        )
                    return response
        except ReviewsRetryableError as exc:
            raise ReviewsUnavailableException(
                f"Reviews service unavailable after {self._max_attempts} attempts: {exc}"
            ) from exc

        raise ReviewsUnavailableException(
            f"Reviews service unavailable after {self._max_attempts} attempts"
        )
