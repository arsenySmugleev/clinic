import asyncio
import logging
import random
from uuid import UUID

import httpx

from src.schemas.review import ReviewNestedCreate, ReviewResponse

logger = logging.getLogger(__name__)


class ReviewsClient:

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 2.0,
        max_attempts: int = 3,
        backoff_base_seconds: float = 0.2,
        backoff_jitter_seconds: float = 0.1,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_jitter = backoff_jitter_seconds
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def list_by_doctor(self, doctor_id: UUID) -> list[ReviewResponse]:
        try:
            response = await self._request(
                "GET",
                f"/api/v1/doctors/{doctor_id}/reviews",
            )
            return [ReviewResponse.model_validate(item) for item in response.json()]
        except Exception:
            logger.exception(f"Failed to fetch reviews for doctor {doctor_id}")
            return []

    async def create_review(
        self,
        doctor_id: UUID,
        review: ReviewNestedCreate,
    ) -> ReviewResponse | None:
        payload = {
            "doctor_id": str(doctor_id),
            "author_name": review.author_name,
            "rating": review.rating,
            "text": review.text,
        }
        try:
            response = await self._request("POST", "/api/v1/reviews", json=payload)
            return ReviewResponse.model_validate(response.json())
        except Exception:
            logger.exception(f"Failed to create review for doctor {doctor_id}")
            return None

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> httpx.Response:
        last_error: Exception | None = None

        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
                if response.status_code in {502, 503, 504}:
                    raise httpx.HTTPStatusError(
                        f"retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code if exc.response is not None else None
                    if status is not None and status < 500 and status not in {502, 503, 504}:
                        raise
                if attempt >= self._max_attempts:
                    break
                delay = self._backoff_base * (2 ** (attempt - 1))
                delay += random.uniform(0, self._backoff_jitter)
                logger.warning(
                    f"Reviews request {method} {path} failed "
                    f"(attempt {attempt}/{self._max_attempts}): {exc}; "
                    f"retry in {delay:.3f}s"
                )
                await asyncio.sleep(delay)

        assert last_error is not None
        raise last_error
