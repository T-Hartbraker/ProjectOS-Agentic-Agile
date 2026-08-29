"""HTTP bind policy tests."""

from __future__ import annotations

import pytest

from projectos.errors import OrchestrationError
from projectos.http.bind_policy import ensure_safe_bind, is_loopback_host


def test_loopback_hosts_recognized() -> None:
    assert is_loopback_host("127.0.0.1")
    assert is_loopback_host("localhost")
    assert not is_loopback_host("0.0.0.0")


def test_unauthenticated_non_loopback_bind_rejected() -> None:
    with pytest.raises(OrchestrationError):
        ensure_safe_bind(host="0.0.0.0", auth_required=False)


def test_unauthenticated_loopback_allowed() -> None:
    ensure_safe_bind(host="127.0.0.1", auth_required=False)
