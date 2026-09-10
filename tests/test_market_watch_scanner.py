import asyncio

from tradebot.scanner import ManagedScanner, ScannerState


def test_scanner_redacts_cycle_failure_and_stays_stoppable():
    async def case():
        calls = 0

        async def failing_cycle():
            nonlocal calls
            calls += 1
            raise RuntimeError("provider included a secret detail")

        state = ScannerState()
        scanner = ManagedScanner(state, failing_cycle, interval_seconds=0.01)
        scanner.start()
        await asyncio.sleep(0.03)
        await scanner.shutdown()
        assert calls >= 1
        assert "secret" not in (state.last_cycle_error or "")

    asyncio.run(case())
