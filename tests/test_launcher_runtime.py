from __future__ import annotations

import asyncio

import pytest

from app import launcher


class FakeTelegramRuntime:
    def __init__(self, _settings) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class FakeWorkerService:
    instances: list["FakeWorkerService"] = []

    def __init__(self, _settings, telegram_runtime=None) -> None:
        self.telegram_runtime = telegram_runtime
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        FakeWorkerService.instances.append(self)

    async def run_forever(self) -> None:
        self.started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise


@pytest.mark.asyncio
async def test_run_client_launcher_keeps_worker_alive_when_bot_control_plane_fails(
    isolated_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeWorkerService.instances.clear()

    async def fake_authorize() -> None:
        return None

    async def fake_run_supervised_polling(*_args, **_kwargs) -> None:
        raise RuntimeError("internal bot control plane failure")

    monkeypatch.setattr(launcher, "load_settings", lambda: isolated_settings)
    monkeypatch.setattr(launcher, "configure_logging", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_ensure_authorized_session", fake_authorize)
    monkeypatch.setattr(launcher, "run_migrations", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "TelegramRuntime", FakeTelegramRuntime)
    monkeypatch.setattr(launcher, "WorkerService", FakeWorkerService)
    monkeypatch.setattr(launcher, "StoryJobService", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(launcher, "MediaPreparationService", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(launcher, "run_supervised_polling", fake_run_supervised_polling)

    task = asyncio.create_task(launcher.run_client_launcher())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert FakeWorkerService.instances
    worker = FakeWorkerService.instances[0]
    await asyncio.wait_for(worker.started.wait(), timeout=1)
    assert not task.done()
    assert not worker.cancelled.is_set()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker.cancelled.is_set()
