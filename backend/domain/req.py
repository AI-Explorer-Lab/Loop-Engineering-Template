from typing import Literal

from pydantic import BaseModel, Field, field_validator
from typing import Any


class TaskCreateRequest(BaseModel):
    requirement: str = Field(min_length=1, max_length=20_000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=50)

    @field_validator("requirement")
    @classmethod
    def validate_requirement(cls, value: str) -> str:
        requirement = value.strip()
        if not requirement:
            raise ValueError("requirement cannot be blank")
        return requirement

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_acceptance_criteria(cls, values: list[str]) -> list[str]:
        normalized = [str(value).strip() for value in values]
        if not normalized or any(not value for value in normalized):
            raise ValueError(
                "acceptance_criteria must contain at least one non-empty string"
            )
        if any(len(value) > 4_000 for value in normalized):
            raise ValueError("each acceptance criterion must be at most 4000 characters")
        return normalized


class QueueSubtaskCreateRequest(TaskCreateRequest):
    pass


class QueueCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    subtasks: list[QueueSubtaskCreateRequest] = Field(min_length=2, max_length=50)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        name = value.strip()
        if not name:
            raise ValueError("name cannot be blank")
        return name


class ReviewRequest(BaseModel):
    decision: Literal["approved", "changes_requested", "rejected"]
    reviewer: str = Field(min_length=1, max_length=200)
    comment: str = Field(default="", max_length=10_000)
    reviewed_diff_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit_subject: str = Field(default="", max_length=200)

    @field_validator("reviewer")
    @classmethod
    def validate_reviewer(cls, value: str) -> str:
        reviewer = value.strip()
        if not reviewer:
            raise ValueError("reviewer cannot be blank")
        return reviewer

    @field_validator("comment")
    @classmethod
    def normalize_comment(cls, value: str) -> str:
        return value.strip()

    @field_validator("commit_subject")
    @classmethod
    def normalize_commit_subject(cls, value: str) -> str:
        subject = value.strip()
        if "\n" in subject or "\r" in subject:
            raise ValueError("commit_subject must be a single line")
        return subject


class PublishRequest(BaseModel):
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    reviewer: str = Field(min_length=1, max_length=200)

    @field_validator("reviewer")
    @classmethod
    def validate_reviewer(cls, value: str) -> str:
        reviewer = value.strip()
        if not reviewer:
            raise ValueError("reviewer cannot be blank")
        return reviewer


class QueueReorderRequest(BaseModel):
    task_ids: list[str] = Field(min_length=1, max_length=50)
    expected_updated_at: str | None = None

    @field_validator("task_ids")
    @classmethod
    def validate_task_ids(cls, values: list[str]) -> list[str]:
        normalized = [str(value).strip() for value in values]
        if any(not value for value in normalized) or len(set(normalized)) != len(
            normalized
        ):
            raise ValueError("task_ids must contain unique non-empty identifiers")
        return normalized


class NotificationSettingsRequest(BaseModel):
    in_app: bool = True
    browser: bool = True


class ProjectCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    repo_path: str = Field(min_length=1, max_length=2_000)
    project_type: str = Field(default="python", min_length=1, max_length=30)
    validation_options: list[str] = Field(default_factory=list, max_length=10)
    knowledge_actor_id: str = Field(min_length=1, max_length=100)
    backend_architecture_enabled: bool = False

    @field_validator("name", "repo_path")
    @classmethod
    def normalize_project_value(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("project value cannot be blank")
        return normalized

    @field_validator("project_type", "knowledge_actor_id")
    @classmethod
    def normalize_project_option(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("project option cannot be blank")
        return normalized

    @field_validator("validation_options")
    @classmethod
    def normalize_validation_options(cls, values: list[str]) -> list[str]:
        normalized = [str(value).strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("validation options cannot be blank")
        if len(set(normalized)) != len(normalized):
            raise ValueError("validation options must be unique")
        return normalized


class QueueVersionRequest(BaseModel):
    expected_updated_at: str | None = None


class PlanCreateRequest(BaseModel):
    name: str = Field(default="", max_length=500)
    requirement: str = Field(min_length=1, max_length=20_000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("name")
    @classmethod
    def validate_plan_name(cls, value: str) -> str:
        return value.strip()

    @field_validator("requirement")
    @classmethod
    def validate_plan_requirement(cls, value: str) -> str:
        requirement = value.strip()
        if not requirement:
            raise ValueError("requirement cannot be blank")
        return requirement

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_optional_acceptance_criteria(cls, values: list[str]) -> list[str]:
        normalized = [str(value).strip() for value in values]
        if any(not value for value in normalized):
            raise ValueError("acceptance_criteria cannot contain blank strings")
        if any(len(value) > 4_000 for value in normalized):
            raise ValueError("each acceptance criterion must be at most 4000 characters")
        return normalized


class PlanConfirmRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=200)
    edited_draft: dict[str, Any] | None = None

    @field_validator("reviewer")
    @classmethod
    def validate_plan_reviewer(cls, value: str) -> str:
        reviewer = value.strip()
        if not reviewer:
            raise ValueError("reviewer cannot be blank")
        return reviewer
