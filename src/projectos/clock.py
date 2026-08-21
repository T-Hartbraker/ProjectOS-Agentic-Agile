"""Injectable clock for deterministic schedule/daemon tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

Clock = Callable[[], datetime]


def system_utc_now() -> datetime:
    return datetime.now(timezone.utc)


class FakeClock:
    """Mutable clock that never touches the OS clock."""

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> datetime:
        from datetime import timedelta

        self._now = self._now + timedelta(**kwargs)
        return self._now

    def set(self, when: datetime) -> datetime:
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        self._now = when
        return self._now
