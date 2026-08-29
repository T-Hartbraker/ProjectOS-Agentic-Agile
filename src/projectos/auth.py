"""Local control-plane authz. No cloud identity."""

from __future__ import annotations

from dataclasses import dataclass

from projectos.errors import AuthorizationError

ROLES = frozenset({"reader", "operator", "admin", "approver", "slack", "local"})

ROLE_CAPS: dict[str, frozenset[str]] = {
    "reader": frozenset({"read"}),
    "operator": frozenset({"read", "operate"}),
    "admin": frozenset({"read", "operate", "admin"}),
    "approver": frozenset({"read", "approve"}),
    "slack": frozenset({"read", "slack"}),
    "local": frozenset({"read", "operate", "admin", "approve", "slack"}),
}


@dataclass(frozen=True)
class Actor:
    actor_id: str
    role: str

    def allows(self, capability: str) -> bool:
        caps = ROLE_CAPS.get(self.role, frozenset())
        if capability == "slack":
            return "slack" in caps or "operate" in caps
        return capability in caps


@dataclass(frozen=True)
class AuthPolicy:
    required: bool
    actors: dict[str, str]

    def resolve(self, actor_header: str | None) -> Actor:
        raw = str(actor_header or "").strip()
        if not self.required:
            if not raw:
                return Actor("local", "local")
            role = self.actors.get(raw, "local")
            if role not in ROLE_CAPS:
                role = "local"
            return Actor(raw, role)
        if not raw:
            raise AuthorizationError("X-ProjectOS-Actor is required")
        role = self.actors.get(raw)
        if role is None:
            raise AuthorizationError(f"actor {raw!r} is not registered")
        if role not in ROLE_CAPS:
            raise AuthorizationError(f"actor {raw!r} has an unknown role")
        return Actor(raw, role)


def required_capability(method: str, path: str) -> str | None:
    normalized = path.rstrip("/") or "/"
    method = method.upper()
    if method == "OPTIONS":
        return None
    if normalized in {"/health", "/v1/health"}:
        return None
    if normalized.endswith("/integrations/slack/slash"):
        return None
    if method in {"GET", "HEAD"}:
        return "read"
    if "/decisions/" in normalized and (
        normalized.endswith("/approve") or normalized.endswith("/reject")
    ):
        return "approve"
    if "/learning/memories/" in normalized:
        return "admin"
    if normalized.endswith("/recovery/execute") or normalized.endswith("/disable"):
        return "admin"
    if normalized.endswith("/integrations/slack/unbind"):
        return "admin"
    if normalized.endswith("/integrations/slack/inbound") or normalized.endswith(
        "/integrations/slack/command"
    ):
        return "slack"
    if normalized == "/v1/projects" and method == "POST":
        return "admin"
    if normalized.startswith("/v1/settings/") and method in {"PUT", "PATCH", "DELETE", "POST"}:
        return "admin"
    return "operate"


def default_policy() -> AuthPolicy:
    return AuthPolicy(required=False, actors={})
