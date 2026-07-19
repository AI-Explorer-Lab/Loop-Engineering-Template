from ..constant.enums import ErrorCode


class BusinessException(Exception):
    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.BUSINESS_ERROR,
        status_code: int = 400,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code


class InvalidTaskIdError(BusinessException):
    def __init__(self) -> None:
        super().__init__(
            "Invalid task ID",
            code=ErrorCode.INVALID_TASK_ID,
            status_code=400,
        )


class TaskNotFoundError(BusinessException):
    def __init__(self, task_id: str) -> None:
        super().__init__(
            f"Task not found: {task_id}",
            code=ErrorCode.TASK_NOT_FOUND,
            status_code=404,
        )


class TaskConflictError(BusinessException):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code=ErrorCode.TASK_CONFLICT,
            status_code=409,
        )


class TaskNotReadyError(BusinessException):
    def __init__(self, task_id: str) -> None:
        super().__init__(
            f"Task is still running: {task_id}",
            code=ErrorCode.TASK_NOT_READY,
            status_code=409,
        )


class ReviewConflictError(BusinessException):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            code=ErrorCode.TASK_CONFLICT,
            status_code=409,
        )


class InvalidQueueIdError(BusinessException):
    def __init__(self) -> None:
        super().__init__(
            "Invalid queue ID",
            code=ErrorCode.INVALID_QUEUE_ID,
            status_code=400,
        )


class QueueNotFoundError(BusinessException):
    def __init__(self, queue_id: str) -> None:
        super().__init__(
            f"Queue not found: {queue_id}",
            code=ErrorCode.QUEUE_NOT_FOUND,
            status_code=404,
        )


class QueueNotReadyError(BusinessException):
    def __init__(self, queue_id: str) -> None:
        super().__init__(
            f"Queue output is not ready: {queue_id}",
            code=ErrorCode.QUEUE_NOT_READY,
            status_code=409,
        )


class ProjectNotFoundError(BusinessException):
    def __init__(self, project_id: str) -> None:
        super().__init__(
            f"Project not found: {project_id}",
            code=ErrorCode.PROJECT_NOT_FOUND,
            status_code=404,
        )
