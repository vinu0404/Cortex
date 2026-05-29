class AppError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class NotFoundError(AppError):
    def __init__(self, resource: str, resource_id: str = ""):
        detail = f"{resource} not found" + (f": {resource_id}" if resource_id else "")
        super().__init__("NOT_FOUND", detail, 404)


class ConflictError(AppError):
    def __init__(self, message: str):
        super().__init__("CONFLICT", message, 409)


class UnauthorizedError(AppError):
    def __init__(self, message: str = "Unauthorized"):
        super().__init__("UNAUTHORIZED", message, 401)


class ForbiddenError(AppError):
    def __init__(self, message: str = "Forbidden"):
        super().__init__("FORBIDDEN", message, 403)


class ValidationError(AppError):
    def __init__(self, message: str):
        super().__init__("VALIDATION_ERROR", message, 422)


class RateLimitedError(AppError):
    def __init__(self, message: str = "Rate limit exceeded"):
        super().__init__("RATE_LIMITED", message, 429)


class ServiceUnavailableError(AppError):
    def __init__(self, message: str = "Service unavailable"):
        super().__init__("SERVICE_UNAVAILABLE", message, 503)


class TokenBudgetExceededError(AppError):
    def __init__(self, period: str = "daily"):
        super().__init__("TOKEN_BUDGET_EXCEEDED", f"{period} token budget exceeded", 429)


class CircularDependencyError(AppError):
    def __init__(self):
        super().__init__("CIRCULAR_DEPENDENCY", "Circular dependency detected in agent plan", 422)


class PlanValidationError(AppError):
    def __init__(self, message: str):
        super().__init__("PLAN_VALIDATION_ERROR", message, 422)
