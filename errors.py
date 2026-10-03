"""A single error type so every failure reaches the client in the same shape:

    {"error": {"code": "version_conflict", "message": "...", ...extra}}
"""


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}


def not_found(what: str = "Resource") -> ApiError:
    return ApiError(404, "not_found", f"{what} not found")


def forbidden(message: str) -> ApiError:
    return ApiError(403, "forbidden", message)


def bad_request(message: str, code: str = "invalid_request") -> ApiError:
    return ApiError(400, code, message)
