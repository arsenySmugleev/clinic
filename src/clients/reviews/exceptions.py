class ReviewsClientError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ReviewsRetryableError(ReviewsClientError):
    pass
