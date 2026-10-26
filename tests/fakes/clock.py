from __future__ import annotations

from datetime import UTC, datetime, timedelta


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 10, 24, 5, 0, 0, tzinfo=UTC)
        self._mono = 1000.0

    def utc_now(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self._mono

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)
        self._mono += seconds

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        import anyio
        await anyio.sleep(0)
