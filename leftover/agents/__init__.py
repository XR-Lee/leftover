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

log = logging.getLogger("leftover.agents")

START_TIMEOUT = 180  # first ACP launch may npx-download an adapter
_RUNNER_CONTROL_TIMEOUT = 10.0
RUNNER_QUEUE_TIMEOUT = 30.0
POOL_TRANSITION_TIMEOUT = 30.0

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


class _HardAwaitTimeout(TimeoutError):
    """The wrapper deadline expired, rather than the awaitable failing."""


class _HardAwaitCancelled(asyncio.CancelledError):
    """The caller was cancelled while the wrapped awaitable was pending."""


async def _await_hard(awaitable: Awaitable[Any], timeout: float) -> Any:
    """Bound the caller without extending the deadline for task cleanup."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _pending = await asyncio.wait({task}, timeout=max(timeout, 0.0))
    except asyncio.CancelledError:
        _cancel_and_drain(task)
        raise _HardAwaitCancelled from None
    except BaseException:
        _cancel_and_drain(task)
        raise
    if task not in done:
        _cancel_and_drain(task)
        raise _HardAwaitTimeout
    return task.result()


async def _close_runner(runner: BaseRunner, timeout: float | None = None) -> bool:
    """Close within the bound; return False only when the bound is exceeded."""
    limit = (_RUNNER_CONTROL_TIMEOUT if timeout is None
             else min(_RUNNER_CONTROL_TIMEOUT, max(timeout, 0.0)))
    if limit <= 0:
        return False
    try:
        await _await_hard(runner.close(), limit)
        return True
    except _HardAwaitCancelled:
        raise
    except asyncio.CancelledError as exc:
        label = getattr(getattr(runner, "spec", None), "label", "runner")
        log.warning("%s: runner close failed: %s", label,
                    str(exc) or type(exc).__name__)
        return True
    except _HardAwaitTimeout:
        return False
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask an answer
        label = getattr(getattr(runner, "spec", None), "label", "runner")
        log.warning("%s: runner close failed: %s", label, exc)
        return True


async def _cancel_runner(runner: BaseRunner, timeout: float | None = None) -> bool:
    limit = (_RUNNER_CONTROL_TIMEOUT if timeout is None
             else min(_RUNNER_CONTROL_TIMEOUT, max(timeout, 0.0)))
    if limit <= 0:
        return False
    try:
        await _await_hard(runner.cancel(), limit)
        return True
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return False


class _RunnerQueueTimeout(TimeoutError):
    pass


class _PoolShutdownInterrupted(RuntimeError):
    pass


class _PoolTransitionTimeout(TimeoutError):
    pass


def _transition_timeout(operation: str, timeout: float) -> _PoolTransitionTimeout:
    return _PoolTransitionTimeout(
        f"agent pool {operation} timed out after {timeout:g}s")


def _remaining(deadline: float, operation: str,
               timeout: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _transition_timeout(operation, timeout)
    return remaining


def _release_abandoned_acquire(task: asyncio.Future[Any],
                               lock: asyncio.Lock) -> None:
    """Release a lock if its detached acquire won a cancel/timeout race."""
    try:
        acquired = task.result()
    except BaseException:
        return
    if acquired and lock.locked():
        lock.release()


async def _acquire_lock_hard(lock: asyncio.Lock, timeout: float) -> bool:
    """Acquire without Python 3.10 wait_for's cancellation race."""
    task = asyncio.create_task(lock.acquire())
    try:
        done, _pending = await asyncio.wait(
            {task}, timeout=max(timeout, 0.0))
    except BaseException:
        if not task.done():
            task.cancel()
        task.add_done_callback(
            lambda completed: _release_abandoned_acquire(completed, lock))
        raise
    if task not in done:
        if not task.done():
            task.cancel()
        task.add_done_callback(
            lambda completed: _release_abandoned_acquire(completed, lock))
        return False
    return bool(task.result())


async def _acquire_before(lock: asyncio.Lock, deadline: float,
                          operation: str, timeout: float) -> None:
    remaining = _remaining(deadline, operation, timeout)
    if not await _acquire_lock_hard(lock, remaining):
        raise _transition_timeout(operation, timeout) from None


async def _condition_wait_before(
        condition: asyncio.Condition, predicate, deadline: float,
        operation: str, timeout: float) -> None:
    """Wait on an owned condition until its predicate or total deadline."""
    remaining = _remaining(deadline, operation, timeout)
    expired = False

    async def wake_at_deadline() -> None:
        nonlocal expired
        await asyncio.sleep(remaining)
        async with condition:
            expired = True
            condition.notify_all()

    timer = asyncio.create_task(wake_at_deadline())
    try:
        while not predicate():
            if expired or time.monotonic() >= deadline:
                raise _transition_timeout(operation, timeout)
            await condition.wait()
    finally:
        _cancel_and_drain(timer)


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
    async def write(self, before_wait=None, *, deadline: float | None = None,
                    operation: str = "transition", timeout: float = 0.0):
        entered = False
        async with self._condition:
            self._waiting_writers += 1
            self._condition.notify_all()
        try:
            if before_wait is not None:
                await before_wait()
            async with self._condition:
                if deadline is None:
                    await self._condition.wait_for(
                        lambda: not self._writer and self._readers == 0)
                else:
                    await _condition_wait_before(
                        self._condition,
                        lambda: not self._writer and self._readers == 0,
                        deadline,
                        operation,
                        timeout,
                    )
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
        self._shutdown_lock = asyncio.Lock()
        self._cancel_lock = asyncio.Lock()
        self._shutdown_epoch = 0
        self._shutdown_active = False
        self._operations = _OperationGate()

    @property
    def workdir(self) -> str:
        return self._workdir

    async def set_workdir(self, path: str) -> None:
        timeout = POOL_TRANSITION_TIMEOUT
        deadline = time.monotonic() + timeout
        acquired = False
        try:
            await _acquire_before(
                self._lock, deadline, "workdir switch", timeout)
            acquired = True
            if path == self._workdir:
                return
            async with self._operations.write(
                    deadline=deadline, operation="workdir switch",
                    timeout=timeout):
                await self._shutdown_unlocked(
                    deadline, "workdir switch", timeout)
                self._workdir = path
        finally:
            if acquired:
                self._lock.release()

    def peek(self, spec: AgentSpec) -> BaseRunner | None:
        """The live runner for this agent, if one has already been started."""
        return self._runners.get(spec.key)

    def _runner_lock(self, key: str) -> asyncio.Lock:
        return self._runner_locks.setdefault(key, asyncio.Lock())

    def _check_shutdown_epoch(self, epoch: int, spec: AgentSpec) -> None:
        if self._shutdown_active or epoch != self._shutdown_epoch:
            raise _PoolShutdownInterrupted(
                f"{spec.label}: pool shutdown interrupted queued request")

    @contextlib.asynccontextmanager
    async def _runner_slot(self, spec: AgentSpec):
        lock = self._runner_lock(spec.key)
        acquired = False
        try:
            acquired = await _acquire_lock_hard(
                lock, RUNNER_QUEUE_TIMEOUT)
            if not acquired:
                raise _RunnerQueueTimeout(
                    f"{spec.label}: runner queue wait exceeded "
                    f"{RUNNER_QUEUE_TIMEOUT:g}s") from None
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
        epoch = self._shutdown_epoch
        async with self._runner_slot(spec):
            async with self._operations.read():
                self._check_shutdown_epoch(epoch, spec)
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
        epoch = self._shutdown_epoch
        async with self._runner_slot(spec):
            async with self._operations.read():
                self._check_shutdown_epoch(epoch, spec)
                return await self._prepare_unlocked(spec)

    async def run(self, spec: AgentSpec, prompt: str,
                  on_event: OnEvent | None = None) -> Turn:
        queued_at = time.monotonic()
        epoch = self._shutdown_epoch
        try:
            async with self._runner_slot(spec):
                async with self._operations.read():
                    self._check_shutdown_epoch(epoch, spec)
                    runner = await self._prepare_unlocked(spec)
                    turn = await runner.run(prompt, on_event)
                    fallback_error = self._fallback_errors.pop(spec.key, None)
                    if fallback_error and not turn.ok and turn.error:
                        turn.error = (f"acp start failed ({fallback_error}); "
                                      f"exec fallback: {turn.error}")
                    return turn
        except (_RunnerQueueTimeout, _PoolShutdownInterrupted) as exc:
            meta = {"not_executed": True}
            if isinstance(exc, _RunnerQueueTimeout):
                meta["queue_timeout"] = True
            else:
                meta["shutdown_interrupted"] = True
            return Turn(
                agent=spec,
                error=f"not executed: {exc}",
                seconds=time.monotonic() - queued_at,
                meta=meta,
            )

    async def cancel_all(self) -> None:
        timeout = POOL_TRANSITION_TIMEOUT
        await self._cancel_all_unlocked(
            time.monotonic() + timeout, "cancel", timeout)

    async def _cancel_all_unlocked(
            self, deadline: float | None = None, operation: str = "cancel",
            timeout: float = POOL_TRANSITION_TIMEOUT) -> None:
        if deadline is None:
            deadline = time.monotonic() + timeout
        acquired = False
        try:
            await _acquire_before(
                self._cancel_lock, deadline, operation, timeout)
            acquired = True
            remaining = _remaining(deadline, operation, timeout)
            await asyncio.gather(*(
                _cancel_runner(r, remaining)
                for r in list(self._runners.values())),
                return_exceptions=True)
        finally:
            if acquired:
                self._cancel_lock.release()

    async def shutdown(self) -> None:
        timeout = POOL_TRANSITION_TIMEOUT
        deadline = time.monotonic() + timeout
        shutdown_acquired = False
        transition_acquired = False
        try:
            await _acquire_before(
                self._shutdown_lock, deadline, "shutdown", timeout)
            shutdown_acquired = True
            self._shutdown_epoch += 1
            self._shutdown_active = True
            try:
                # Cancellation is an interrupt path: it must not queue behind a
                # pending workdir writer that is itself waiting for the run.
                await self._cancel_all_unlocked(
                    deadline, "shutdown", timeout)
                await _acquire_before(
                    self._lock, deadline, "shutdown", timeout)
                transition_acquired = True
                async with self._operations.write(
                        deadline=deadline, operation="shutdown",
                        timeout=timeout):
                    await self._shutdown_unlocked(
                        deadline, "shutdown", timeout)
            finally:
                if transition_acquired:
                    self._lock.release()
                self._shutdown_active = False
                self._shutdown_epoch += 1
        finally:
            if shutdown_acquired:
                self._shutdown_lock.release()

    async def _shutdown_unlocked(
            self, deadline: float | None = None,
            operation: str = "shutdown",
            timeout: float = POOL_TRANSITION_TIMEOUT) -> None:
        if deadline is None:
            deadline = time.monotonic() + timeout
        acquired = False
        try:
            await _acquire_before(
                self._cancel_lock, deadline, operation, timeout)
            acquired = True
            runners = list(self._runners.values())
            self._runners.clear()
            self._fallback_errors.clear()
            if not runners:
                return
            remaining = _remaining(deadline, operation, timeout)
            results = await asyncio.gather(*(
                _close_runner(r, remaining) for r in runners),
                return_exceptions=True)
            if any(result is not True for result in results):
                raise _transition_timeout(operation, timeout)
        finally:
            if acquired:
                self._cancel_lock.release()
