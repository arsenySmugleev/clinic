from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ReviewNestedCreate(BaseModel):

    author_name: str = Field(min_length=1, max_length=200)
    rating: int = Field(ge=1, le=5)
    text: str = Field(min_length=1)


class ReviewResponse(BaseModel):
    id: UUID
    doctor_id: UUID
    author_name: str
    rating: int
    text: str
    created_at: datetime
