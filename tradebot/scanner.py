from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field


@dataclass
class ScannerState:
    status: str = "initializing"
    last_cycle_error: str | None = None
    cycles_completed: int = 0
    in_flight: set[asyncio.Task] = field(default_factory=set)


class ManagedScanner:
    """PR A scanner shell. It drains durable work only; no providers or discovery."""

    def __init__(
        self,
        state: ScannerState,
        cycle: Callable[[], Awaitable[None]] | None = None,
        interval_seconds: float = 1.0,
    ):
        self.state = state
        self.cycle = cycle
        self.interval_seconds = interval_seconds
        self.stop_event = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self.state.status = "active"
        self._task = asyncio.create_task(self.run(), name="managed_scanner")

    async def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                if self.cycle:
                    await self.cycle()
                self.state.cycles_completed += 1
                self.state.last_cycle_error = None
            except Exception as exc:  # noqa: BLE001 - scanner boundary records a redacted category
                self.state.last_cycle_error = f"cycle_error:{type(exc).__name__}"
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(), timeout=self.interval_seconds
                )
            except TimeoutError:
                continue

    async def shutdown(self, timeout_seconds: float = 5.0) -> None:
        self.stop_event.set()
        pending = [
            task
            for task in [self._task, *self.state.in_flight]
            if task and not task.done()
        ]
        if pending:
            _done, still_pending = await asyncio.wait(pending, timeout=timeout_seconds)
            for task in still_pending:
                task.cancel()
            if still_pending:
                await asyncio.gather(*still_pending, return_exceptions=True)
        self.state.status = "stopped"
