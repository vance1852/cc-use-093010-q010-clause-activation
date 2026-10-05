"""条款跟踪服务向 API 和 CLI 暴露的稳定错误。"""


class ClauseError(RuntimeError):
    code = "clause_error"
    status = 400


class NotFound(ClauseError):
    code = "not_found"
    status = 404


class Conflict(ClauseError):
    code = "conflict"
    status = 409


class Forbidden(ClauseError):
    code = "forbidden"
    status = 403


class InvalidState(ClauseError):
    code = "invalid_state"
    status = 409


class ValidationFailed(ClauseError):
    code = "validation_failed"
    status = 422
