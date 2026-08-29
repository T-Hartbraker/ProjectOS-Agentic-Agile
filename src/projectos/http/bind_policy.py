"""HTTP bind safety — prevent unauthenticated exposure beyond loopback."""

from __future__ import annotations

from projectos.errors import OrchestrationError

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def is_loopback_host(host: str) -> bool:
    normalized = str(host or "").strip().lower()
    if normalized in _LOOPBACK_HOSTS:
        return True
    if normalized.startswith("127."):
        return True
    return False


def ensure_safe_bind(*, host: str, auth_required: bool) -> None:
    """Fail startup when unauthenticated API would listen beyond loopback."""
    if auth_required:
        return
    if is_loopback_host(host):
        return
    raise OrchestrationError(
        "Refusing to start unauthenticated ProjectOS HTTP API on non-loopback host "
        f"{host!r}. Bind to 127.0.0.1/localhost or enable authentication."
    )
