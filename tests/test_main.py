import asyncio
import logging
import time

import pytest

import main as main_module


class WaitingEngine:
    def __init__(self):
        self.stop = asyncio.Event()
        self.stop_requests = 0

    def request_stop(self):
        self.stop_requests += 1
        self.stop.set()

    async def run(self):
        await self.stop.wait()


def test_dashboard_failure_stops_engine_and_propagates():
    class BrokenDashboard:
        @staticmethod
        async def run():
            raise RuntimeError("dashboard exploded")

    async def go():
        eng = WaitingEngine()

        with pytest.raises(RuntimeError, match="dashboard exploded"):
            await asyncio.wait_for(
                main_module._run_with_dashboard(eng, BrokenDashboard()),
                timeout=0.2,
            )

        assert eng.stop.is_set()
        assert eng.stop_requests >= 1

    asyncio.run(go())


def test_dashboard_early_return_is_an_error_and_stops_engine():
    class EarlyDashboard:
        @staticmethod
        async def run():
            return None

    async def go():
        eng = WaitingEngine()

        with pytest.raises(RuntimeError, match="dashboard exited unexpectedly"):
            await asyncio.wait_for(
                main_module._run_with_dashboard(eng, EarlyDashboard()),
                timeout=0.2,
            )

        assert eng.stop.is_set()

    asyncio.run(go())


def test_engine_error_is_not_replaced_by_dashboard_cancellation():
    class FailingEngine(WaitingEngine):
        def __init__(self):
            super().__init__()
            self.dashboard_started = asyncio.Event()

        async def run(self):
            await self.dashboard_started.wait()
            raise ValueError("engine exploded")

    class CancelledDashboard:
        def __init__(self, eng):
            self.eng = eng

        async def run(self):
            self.eng.dashboard_started.set()
            await self.eng.stop.wait()
            raise asyncio.CancelledError

    async def go():
        eng = FailingEngine()

        with pytest.raises(ValueError, match="engine exploded"):
            await asyncio.wait_for(
                main_module._run_with_dashboard(
                    eng, CancelledDashboard(eng)),
                timeout=0.2,
            )

    asyncio.run(go())


def test_caller_cancellation_during_dashboard_cleanup_is_preserved():
    class FinishingEngine(WaitingEngine):
        def __init__(self):
            super().__init__()
            self.dashboard_started = asyncio.Event()

        async def run(self):
            await self.dashboard_started.wait()

    class SlowDashboard:
        def __init__(self, eng):
            self.eng = eng
            self.release = asyncio.Event()
            self.closed = asyncio.Event()

        async def run(self):
            self.eng.dashboard_started.set()
            try:
                await self.release.wait()
            finally:
                self.closed.set()

    async def go():
        eng = FinishingEngine()
        dash = SlowDashboard(eng)
        task = asyncio.create_task(
            main_module._run_with_dashboard(eng, dash))
        await eng.dashboard_started.wait()
        await asyncio.sleep(0)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            dash.release.set()
            await dash.closed.wait()

    asyncio.run(go())


def test_simultaneous_caller_and_dashboard_cancellation_preserves_caller():
    class FinishingEngine(WaitingEngine):
        def __init__(self):
            super().__init__()
            self.dashboard_started = asyncio.Event()

        async def run(self):
            await self.dashboard_started.wait()

    class CoordinatedDashboard:
        def __init__(self, eng):
            self.eng = eng
            self.stopping = asyncio.Event()
            self.release = asyncio.Event()

        async def run(self):
            self.eng.dashboard_started.set()
            await self.eng.stop.wait()
            self.stopping.set()
            await self.release.wait()
            raise asyncio.CancelledError

    async def go():
        eng = FinishingEngine()
        dash = CoordinatedDashboard(eng)
        task = asyncio.create_task(
            main_module._run_with_dashboard(eng, dash))
        await dash.stopping.wait()
        dash.release.set()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())


def test_dashboard_cancellation_cleanup_has_a_hard_deadline(monkeypatch):
    monkeypatch.setattr(main_module, "DASHBOARD_STOP_TIMEOUT_SEC", 0.01)

    class FinishingEngine(WaitingEngine):
        async def run(self):
            await self.dashboard_started.wait()

        def __init__(self):
            super().__init__()
            self.dashboard_started = asyncio.Event()

    class StubbornDashboard:
        def __init__(self, eng):
            self.eng = eng
            self.release = asyncio.Event()

        async def run(self):
            self.eng.dashboard_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await self.release.wait()

    async def go():
        eng = FinishingEngine()
        dash = StubbornDashboard(eng)
        try:
            with pytest.raises(RuntimeError, match="did not stop"):
                await asyncio.wait_for(
                    main_module._run_with_dashboard(eng, dash), timeout=0.1)
        finally:
            dash.release.set()
            await asyncio.sleep(0)

    asyncio.run(go())


def test_dashboard_finalizer_error_is_not_discarded(monkeypatch):
    monkeypatch.setattr(main_module, "DASHBOARD_STOP_TIMEOUT_SEC", 0.01)

    class FinishingEngine(WaitingEngine):
        async def run(self):
            await self.dashboard_started.wait()

        def __init__(self):
            super().__init__()
            self.dashboard_started = asyncio.Event()

    class BrokenFinalizerDashboard:
        def __init__(self, eng):
            self.eng = eng

        async def run(self):
            self.eng.dashboard_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                raise ValueError("dashboard finalizer exploded")

    async def go():
        eng = FinishingEngine()

        with pytest.raises(ValueError, match="dashboard finalizer exploded"):
            await asyncio.wait_for(
                main_module._run_with_dashboard(
                    eng, BrokenFinalizerDashboard(eng)), timeout=0.1)

    asyncio.run(go())


def test_engine_error_logs_simultaneous_dashboard_finalizer_error(caplog):
    class FailingEngine(WaitingEngine):
        def __init__(self):
            super().__init__()
            self.dashboard_started = asyncio.Event()

        async def run(self):
            await self.dashboard_started.wait()
            raise ValueError("engine exploded")

    class BrokenFinalizerDashboard:
        def __init__(self, eng):
            self.eng = eng

        async def run(self):
            self.eng.dashboard_started.set()
            try:
                await self.eng.stop.wait()
            finally:
                raise RuntimeError("dashboard finalizer exploded")

    async def go():
        eng = FailingEngine()
        with pytest.raises(ValueError, match="engine exploded"):
            await main_module._run_with_dashboard(
                eng, BrokenFinalizerDashboard(eng))

    caplog.set_level(logging.ERROR)
    asyncio.run(go())

    record = next(
        record for record in caplog.records
        if "dashboard also failed" in record.getMessage())
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)


def test_application_runner_abandons_non_cooperative_dashboard(monkeypatch):
    monkeypatch.setattr(main_module, "DASHBOARD_STOP_TIMEOUT_SEC", 0.005)

    class FinishingEngine(WaitingEngine):
        def __init__(self):
            super().__init__()
            self.dashboard_started = asyncio.Event()

        async def run(self):
            await self.dashboard_started.wait()

    class NonCooperativeDashboard:
        def __init__(self, eng):
            self.eng = eng

        async def run(self):
            self.eng.dashboard_started.set()
            while True:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    continue

    eng = FinishingEngine()
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="did not stop"):
        main_module._run_application(
            main_module._run_with_dashboard(
                eng, NonCooperativeDashboard(eng)))

    assert time.monotonic() - started < 0.2
