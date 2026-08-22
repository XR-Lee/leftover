"""Agent runner construction and pooling."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Awaitable
from typing import Any

from ..config import AgentSpec, Config
from .base import BaseRunner, Event, OnEvent, Runner, Turn
from .exec_runner import ExecRunner

log = logging.getLogger("agora.agents")

START_TIMEOUT = 180  # first ACP launch may npx-download an adapter
_RUNNER_CONTROL_TIMEOUT = 10.0
RUNNER_QUEUE_TIMEOUT = 30.0

__all__ = ["Event", "OnEvent", "Runner", "Turn", "AgentPool", "build_runner"]


def build_runner(spec: AgentSpec) -> BaseRunner:
    if spec.transport == "exec":
        return ExecRunner(spec)
    want_acp = spec.transport == "acp" or (
        spec.transport == "auto" and spec.acp_command
        and shutil.which(spec.acp_command[0]))
    if want_acp:
        from .acp_runner import AcpRunner
        return AcpRunner(spec)
    return ExecRunner(spec)


def _consume_task_result(task: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        task.exception()


def _cancel_and_drain(task: asyncio.Future[Any]) -> None:
    if not task.done():
        task.cancel()
    task.add_done_callback(_consume_task_result)


async def _await_hard(awaitable: Awaitable[Any], timeout: float) -> Any:
    """Bound the caller without extending the deadline for task cleanup."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=max(timeout, 0.0))
    except BaseException:
        _cancel_and_drain(task)
        raise
    if task not in done:
        _cancel_and_drain(task)
        raise TimeoutError
    return task.result()


async def _close_runner(runner: BaseRunner) -> None:
    try:
        await _await_hard(runner.close(), _RUNNER_CONTROL_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        pass


async def _cancel_runner(runner: BaseRunner) -> None:
    try:
        await _await_hard(runner.cancel(), _RUNNER_CONTROL_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        pass


class _RunnerQueueTimeout(TimeoutError):
    pass


class _OperationGate:
    """Writer-preferring async gate for runs versus workdir transitions."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextlib.asynccontextmanager
    async def read(self):
        async with self._condition:
            await self._condition.wait_for(
                lambda: not self._writer and self._waiting_writers == 0)
            self._readers += 1
        try:
            yield
        finally:
            async with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextlib.asynccontextmanager
    async def write(self, before_wait=None):
        entered = False
        async with self._condition:
            self._waiting_writers += 1
            self._condition.notify_all()
        try:
            if before_wait is not None:
                await before_wait()
            async with self._condition:
                await self._condition.wait_for(
                    lambda: not self._writer and self._readers == 0)
                self._waiting_writers -= 1
                self._writer = True
                entered = True
            yield
        finally:
            async with self._condition:
                if entered:
                    self._writer = False
                else:
                    self._waiting_writers -= 1
                self._condition.notify_all()


class AgentPool:
    """Keeps one live runner per agent, rebuilt when the working dir changes."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self._runners: dict[str, BaseRunner] = {}
        self._runner_locks: dict[str, asyncio.Lock] = {}
        self._fallback_errors: dict[str, str] = {}
        self._workdir = config.default_workdir
        self._lock = asyncio.Lock()
        self._operations = _OperationGate()

    @property
    def workdir(self) -> str:
        return self._workdir

    async def set_workdir(self, path: str) -> None:
        async with self._lock:
            if path == self._workdir:
                return
            async with self._operations.write():
                await self._shutdown_unlocked()
                self._workdir = path

    def peek(self, spec: AgentSpec) -> BaseRunner | None:
        """The live runner for this agent, if one has already been started."""
        return self._runners.get(spec.key)

    def _runner_lock(self, key: str) -> asyncio.Lock:
        return self._runner_locks.setdefault(key, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def _runner_slot(self, spec: AgentSpec):
        lock = self._runner_lock(spec.key)
        acquired = False
        try:
            try:
                await asyncio.wait_for(
                    lock.acquire(), timeout=RUNNER_QUEUE_TIMEOUT)
            except asyncio.TimeoutError:
                raise _RunnerQueueTimeout(
                    f"{spec.label}: runner queue wait exceeded "
                    f"{RUNNER_QUEUE_TIMEOUT:g}s") from None
            acquired = True
            yield
        finally:
            if acquired:
                lock.release()

    async def _get_unlocked(self, spec: AgentSpec) -> BaseRunner:
        runner = self._runners.get(spec.key)
        if runner is None:
            runner = build_runner(spec)
            self._runners[spec.key] = runner
        try:
            await _await_hard(runner.start(self._workdir), START_TIMEOUT)
        except TimeoutError:
            raise TimeoutError(
                f"{spec.label}: runner start timed out after {START_TIMEOUT:g}s"
            ) from None
        return runner

    async def get(self, spec: AgentSpec) -> BaseRunner:
        async with self._operations.read():
            async with self._runner_slot(spec):
                return await self._get_unlocked(spec)

    async def _prepare_unlocked(self, spec: AgentSpec) -> BaseRunner:
        """Start one managed runner, installing its exec fallback on failure."""
        try:
            return await self._get_unlocked(spec)
        except Exception as exc:              # noqa: BLE001
            log.warning("%s: ACP start failed (%s); falling back to exec",
                        spec.label, exc)
            failed = self._runners.pop(spec.key, None)
            if failed is not None:
                await _close_runner(failed)
            fallback = ExecRunner(spec)
            await fallback.start(self._workdir)
            self._runners[spec.key] = fallback
            self._fallback_errors[spec.key] = str(exc)
            return fallback

    async def prepare(self, spec: AgentSpec) -> BaseRunner:
        """Single-flight warmup that leaves a runnable ACP or exec backend."""
        async with self._operations.read():
            async with self._runner_slot(spec):
                return await self._prepare_unlocked(spec)

    async def run(self, spec: AgentSpec, prompt: str,
                  on_event: OnEvent | None = None) -> Turn:
        queued_at = time.monotonic()
        async with self._operations.read():
            try:
                async with self._runner_slot(spec):
                    runner = await self._prepare_unlocked(spec)
                    turn = await runner.run(prompt, on_event)
                    fallback_error = self._fallback_errors.pop(spec.key, None)
                    if fallback_error and not turn.ok and turn.error:
                        turn.error = (f"acp start failed ({fallback_error}); "
                                      f"exec fallback: {turn.error}")
                    return turn
            except _RunnerQueueTimeout as exc:
                return Turn(
                    agent=spec,
                    error=f"not executed: {exc}",
                    seconds=time.monotonic() - queued_at,
                    meta={"queue_timeout": True, "not_executed": True},
                )

    async def cancel_all(self) -> None:
        async with self._operations.read():
            await self._cancel_all_unlocked()

    async def _cancel_all_unlocked(self) -> None:
        await asyncio.gather(*(
            _cancel_runner(r) for r in list(self._runners.values())),
            return_exceptions=True)

    async def shutdown(self) -> None:
        async with self._operations.write(
                before_wait=self._cancel_all_unlocked):
            await self._shutdown_unlocked()

    async def _shutdown_unlocked(self) -> None:
        runners = list(self._runners.values())
        self._runners.clear()
        self._fallback_errors.clear()
        await asyncio.gather(*(_close_runner(r) for r in runners),
                             return_exceptions=True)
