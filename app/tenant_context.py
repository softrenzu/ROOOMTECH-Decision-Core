from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass(frozen=True)
class TenantContext:
    project_id: str
    registry: object


_context: ContextVar[TenantContext | None] = ContextVar("rtdc_tenant_context", default=None)


def set_tenant_context(project_id: str, registry: object) -> Token:
    return _context.set(TenantContext(project_id=project_id, registry=registry))


def reset_tenant_context(token: Token) -> None:
    _context.reset(token)


def current_tenant() -> TenantContext | None:
    return _context.get()


def assert_model_access(model_id: str) -> None:
    """Reject cross-project model access when a project context is active.

    Administrative/local execution has no tenant context and therefore remains able to
    inspect resources for operator maintenance and migration.
    """

    context = current_tenant()
    if context is None:
        return
    context.registry.assert_owner(context.project_id, "model", model_id)
