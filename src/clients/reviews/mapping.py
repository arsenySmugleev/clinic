from uuid import UUID

from src.schemas.review import ReviewNestedCreate, ReviewResponse


def map_review(data: dict) -> ReviewResponse:
    return ReviewResponse.model_validate(data)


def map_reviews(items: list) -> list[ReviewResponse]:
    return [map_review(item) for item in items]


def map_create_payload(doctor_id: UUID, review: ReviewNestedCreate) -> dict:
    return {
        "doctor_id": str(doctor_id),
        "author_name": review.author_name,
        "rating": review.rating,
        "text": review.text,
    }
