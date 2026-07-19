from __future__ import annotations

from fastapi import Request

from ..service.project_registry import ProjectContext, ProjectRegistry


def project_context(request: Request) -> ProjectContext | None:
    registry: ProjectRegistry | None = getattr(
        request.app.state, "project_registry", None
    )
    if registry is None:
        return None
    return registry.get(request.headers.get("X-Project-ID"))
