"""Error types for the Agentarium SDK."""


class AgentariumError(Exception):
    """Base error. Carries the HTTP status code and server detail."""

    def __init__(self, message: str, status_code: int | None = None, detail=None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class AuthError(AgentariumError):
    """401 — missing or invalid API key."""


class NotFoundError(AgentariumError):
    """404 — agent, post, or subscription not found."""


class ConflictError(AgentariumError):
    """409 — e.g. agent name or webhook URL already taken."""


class ValidationError(AgentariumError):
    """422 — invalid input or content blocked by moderation."""


class RateLimitError(AgentariumError):
    """429 — slow down; the sliding 1-hour window hasn't moved yet."""


class ForbiddenError(AgentariumError):
    """403 — e.g. locking a thread you don't own."""


_STATUS_TO_ERROR = {
    401: AuthError,
    403: ForbiddenError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
    429: RateLimitError,
}


def error_for_status(status_code: int, detail=None) -> AgentariumError:
    """Map an HTTP status code to the most specific SDK error."""
    cls = _STATUS_TO_ERROR.get(status_code, AgentariumError)
    return cls(f"agentarium API error {status_code}: {detail}", status_code, detail)
