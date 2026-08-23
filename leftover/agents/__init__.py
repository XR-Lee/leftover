"""Agent runner construction and pooling."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from typing import Any

from ..config import AgentSpec, Config
from .base import (
    BaseRunner,
    Event,
    OnEvent,
    Runner,
    Turn,
    TurnHandle,
    TurnState,
)
from .exec_runner import ExecRunner

log = logging.getLogger("leftover.agents")

START_TIMEOUT = 180  # first ACP launch may npx-download an adapter
_RUNNER_CONTROL_TIMEOUT = 10.0
RUNNER_QUEUE_TIMEOUT = 30.0
POOL_TRANSITION_TIMEOUT = 30.0
COMPLETION_INBOX_SIZE = 256

__all__ = [
    "Event", "OnEvent", "Runner", "Turn", "TurnHandle", "TurnState",
    "AgentPool", "build_runner",
]


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


def _task_returned_true(task: asyncio.Task[Any]) -> bool:
    if task.cancelled():
        return False
    try:
        return task.result() is True
    except BaseException:
        return False


def _request_task_cancel(task: asyncio.Future[Any]) -> None:
    if task.done():
        return
    cancelling = getattr(task, "cancelling", None)
    if callable(cancelling) and cancelling():
        return
    task.cancel()


def _cancel_and_drain(task: asyncio.Future[Any]) -> None:
    _request_task_cancel(task)
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


async def _close_runner(
        runner: BaseRunner, timeout: float | None = None,
        track_detached: Callable[[asyncio.Task[Any]], None] | None = None,
        *, wait_detached: bool = False,
        on_bounded: Callable[[], None] | None = None,
        ) -> bool:
    """Bound a close attempt while keeping unfinished cleanup observable."""
    limit = (_RUNNER_CONTROL_TIMEOUT if timeout is None
             else min(_RUNNER_CONTROL_TIMEOUT, max(timeout, 0.0)))
    if limit <= 0:
        if on_bounded is not None:
            on_bounded()
        return False
    task = asyncio.create_task(
        runner.close(), name=f"leftover-close-{runner.spec.key}")

    def detach() -> None:
        if track_detached is not None:
            track_detached(task)
        else:
            task.add_done_callback(_consume_task_result)

    def retain() -> None:
        if wait_detached:
            # The surrounding lifecycle finalizer remains the registry owner
            # while it shields and observes this concrete close attempt.
            task.add_done_callback(_consume_task_result)
        else:
            detach()

    def bounded() -> None:
        if on_bounded is not None:
            on_bounded()

    try:
        done, _pending = await asyncio.wait({task}, timeout=limit)
    except asyncio.CancelledError:
        _request_task_cancel(task)
        retain()
        bounded()
        raise _HardAwaitCancelled from None
    except BaseException:
        _request_task_cancel(task)
        retain()
        bounded()
        raise
    if task not in done:
        _request_task_cancel(task)
        retain()
        bounded()
        if not wait_detached:
            return False
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            owner = asyncio.current_task()
            if owner is not None and owner.cancelling():
                if not task.done():
                    task.cancel()
                raise _HardAwaitCancelled from None
            if not task.done():
                task.cancel()
                raise _HardAwaitCancelled from None
            label = getattr(
                getattr(runner, "spec", None), "label", "runner")
            log.warning("%s: runner close failed: %s", label,
                        str(exc) or type(exc).__name__)
            return False
        except Exception as exc:  # noqa: BLE001
            label = getattr(
                getattr(runner, "spec", None), "label", "runner")
            log.warning("%s: runner close failed: %s", label, exc)
            return False
        return True
    bounded()
    try:
        task.result()
    except asyncio.CancelledError as exc:
        label = getattr(getattr(runner, "spec", None), "label", "runner")
        log.warning("%s: runner close failed: %s", label,
                    str(exc) or type(exc).__name__)
        return False
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask an answer
        label = getattr(getattr(runner, "spec", None), "label", "runner")
        log.warning("%s: runner close failed: %s", label, exc)
        return False
    return True


async def _cancel_runner(
        runner: BaseRunner, timeout: float | None = None,
        track_detached: Callable[[asyncio.Task[Any]], None] | None = None
        ) -> bool:
    limit = (_RUNNER_CONTROL_TIMEOUT if timeout is None
             else min(_RUNNER_CONTROL_TIMEOUT, max(timeout, 0.0)))
    if limit <= 0:
        return False
    task = asyncio.create_task(
        runner.cancel(), name=f"leftover-cancel-{runner.spec.key}")

    def detach() -> None:
        if track_detached is not None:
            track_detached(task)
        else:
            task.add_done_callback(_consume_task_result)

    try:
        done, _pending = await asyncio.wait({task}, timeout=limit)
    except asyncio.CancelledError:
        _request_task_cancel(task)
        detach()
        raise
    except BaseException:
        _request_task_cancel(task)
        detach()
        raise
    if task not in done:
        _request_task_cancel(task)
        detach()
        return False
    try:
        task.result()
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return False
    return True


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
        self._cancel_epoch = 0
        self._cancel_active = False
        self._operations = _OperationGate()
        self._active_turns: dict[str, TurnHandle] = {}
        self._startup_tasks: set[asyncio.Task[Any]] = set()
        self._startup_runners: dict[asyncio.Task[Any], BaseRunner] = {}
        self._detached_startups: set[asyncio.Task[Any]] = set()
        self._startup_finalizers: dict[
            asyncio.Task[Any], asyncio.Task[Any]] = {}
        self._startup_retire_signals: dict[
            asyncio.Task[Any], asyncio.Event] = {}
        self._startup_terminal_signals: dict[
            asyncio.Task[Any], asyncio.Event] = {}
        self._startup_close_signals: dict[
            asyncio.Task[Any], asyncio.Event] = {}
        self._startup_close_timeouts: dict[
            asyncio.Task[Any], float] = {}
        self._startup_lifecycle_runners: dict[
            asyncio.Task[Any], BaseRunner] = {}
        self._runner_startups: dict[BaseRunner, asyncio.Task[Any]] = {}
        self._warmup_owners: set[asyncio.Task[Any]] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._background_expect_true: set[asyncio.Task[Any]] = set()
        self._background_outcomes: dict[asyncio.Task[Any], bool] = {}
        self._completion_inboxes: dict[
            str | None, asyncio.Queue[TurnHandle]] = {
                None: asyncio.Queue(maxsize=COMPLETION_INBOX_SIZE),
            }
        # Backward-compatible private alias for the default owner.
        self._completion_inbox = self._completion_inboxes[None]
        self._completion_overflows: dict[str | None, int] = {}
        self._completion_waiters: dict[str | None, int] = {}

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

    def active_turn(self, turn_id: str) -> TurnHandle | None:
        """Return a submitted turn until its worker cleanup has completed."""
        return self._active_turns.get(turn_id)

    def _runner_lock(self, key: str) -> asyncio.Lock:
        return self._runner_locks.setdefault(key, asyncio.Lock())

    @contextlib.asynccontextmanager
    async def _warmup_owner(self):
        owner = asyncio.current_task()
        if owner is not None:
            self._warmup_owners.add(owner)
        try:
            yield
        finally:
            if owner is not None:
                self._warmup_owners.discard(owner)

    def _track_background_task(
            self, task: asyncio.Task[Any], *, expect_true: bool = False) -> None:
        if expect_true:
            self._background_expect_true.add(task)
        if (task in self._background_tasks
                or task in self._background_outcomes):
            return
        self._background_tasks.add(task)
        task.add_done_callback(self._background_task_finished)

    def _background_task_finished(self, done: asyncio.Task[Any]) -> None:
        if (done not in self._background_tasks
                and done not in self._background_expect_true):
            _consume_task_result(done)
            return
        expected_true = done in self._background_expect_true
        self._background_tasks.discard(done)
        self._background_expect_true.discard(done)
        if expected_true:
            self._background_outcomes[done] = _task_returned_true(done)
        _consume_task_result(done)

    def _reconcile_completed_lifecycle_tasks(self) -> None:
        for start_task, finalizer in list(self._startup_finalizers.items()):
            if finalizer.done() and _task_returned_true(finalizer):
                self._forget_start_lifecycle(start_task, finalizer)
        for task in list(self._background_tasks):
            if task.done():
                self._background_task_finished(task)

    def _close_detached_runner(self, runner: BaseRunner) -> None:
        task = asyncio.create_task(
            _close_runner(
                runner, track_detached=self._track_background_task),
            name=f"leftover-close-detached-{runner.spec.key}",
        )
        self._track_background_task(task, expect_true=True)

    async def _drain_background_tasks(
            self, deadline: float, operation: str, timeout: float) -> None:
        while True:
            await asyncio.sleep(0)
            self._reconcile_completed_lifecycle_tasks()
            outcomes = list(self._background_outcomes.values())
            self._background_outcomes.clear()
            if any(not outcome for outcome in outcomes):
                raise _transition_timeout(operation, timeout)
            tasks = list(self._background_tasks)
            if not tasks:
                return
            remaining = _remaining(deadline, operation, timeout)
            _done, pending = await asyncio.wait(tasks, timeout=remaining)
            if pending:
                # These tasks still own cleanup. A transition timeout bounds the
                # caller, not the cleanup itself; cancelling here can make a
                # stubborn coroutine exit and disappear from the registry before
                # the resource it owns is actually closed.
                raise _transition_timeout(operation, timeout)
            # Let completion callbacks publish outcomes or newly owned work.
            await asyncio.sleep(0)

    def _rollback_warmup_runner(
            self, spec: AgentSpec, initial: BaseRunner | None,
            runner: BaseRunner | None = None) -> None:
        if runner is None:
            runner = self._runners.get(spec.key)
        if runner is None:
            return
        if runner is initial or self._runners.get(spec.key) is not runner:
            return
        self._runners.pop(spec.key, None)
        self._fallback_errors.pop(spec.key, None)
        self._mark_start_detached(runner)

    async def _finalize_detached_start(
            self, start_task: asyncio.Task[Any],
            runner: BaseRunner, retire_signal: asyncio.Event,
            terminal_signal: asyncio.Event,
            close_signal: asyncio.Event) -> bool:
        # The finalizer exists before startup can yield. A pool transition only
        # releases this owner; its deadline never cancels the underlying cleanup.
        await retire_signal.wait()
        close_timeout = self._startup_close_timeouts.get(
            start_task, _RUNNER_CONTROL_TIMEOUT)
        if not terminal_signal.is_set():
            # Interrupt a partially initialized transport immediately. Startup
            # may still ignore cancellation and publish resources afterward, so
            # this first close is only the early half of retirement.
            await _close_runner(
                runner, close_timeout,
                track_detached=self._track_background_task,
                wait_detached=True, on_bounded=close_signal.set)
        await terminal_signal.wait()
        # Always close again after startup becomes terminal. This catches a
        # transport that finished opening after the early close raced with it.
        result = await _close_runner(
            runner, close_timeout,
            track_detached=self._track_background_task,
            wait_detached=True, on_bounded=close_signal.set)
        return result

    def _forget_start_lifecycle(
            self, start_task: asyncio.Task[Any],
            finalizer: asyncio.Task[Any]) -> None:
        if self._startup_finalizers.get(start_task) is not finalizer:
            return
        runner = self._startup_lifecycle_runners.pop(start_task, None)
        self._startup_finalizers.pop(start_task, None)
        self._startup_retire_signals.pop(start_task, None)
        self._startup_terminal_signals.pop(start_task, None)
        self._startup_close_signals.pop(start_task, None)
        self._startup_close_timeouts.pop(start_task, None)
        self._detached_startups.discard(start_task)
        if (runner is not None
                and self._runner_startups.get(runner) is start_task):
            self._runner_startups.pop(runner, None)

    def _create_start_finalizer(
            self, start_task: asyncio.Task[Any], runner: BaseRunner,
            retire_signal: asyncio.Event,
            terminal_signal: asyncio.Event,
            close_signal: asyncio.Event) -> asyncio.Task[Any]:
        finalizer = asyncio.create_task(
            self._finalize_detached_start(
                start_task, runner, retire_signal, terminal_signal,
                close_signal),
            name=f"leftover-finalize-start-{runner.spec.key}",
        )
        self._startup_finalizers[start_task] = finalizer

        def finalized(done: asyncio.Task[Any]) -> None:
            if _task_returned_true(done):
                self._forget_start_lifecycle(start_task, done)
            _consume_task_result(done)

        finalizer.add_done_callback(finalized)
        return finalizer

    def _retry_failed_start_finalizers(self) -> None:
        for start_task in list(self._detached_startups):
            finalizer = self._startup_finalizers.get(start_task)
            if finalizer is None or not finalizer.done():
                continue
            if _task_returned_true(finalizer):
                self._forget_start_lifecycle(start_task, finalizer)
                continue
            runner = self._startup_lifecycle_runners.get(start_task)
            retire_signal = self._startup_retire_signals.get(start_task)
            terminal_signal = self._startup_terminal_signals.get(start_task)
            close_signal = self._startup_close_signals.get(start_task)
            if (runner is None or retire_signal is None
                    or terminal_signal is None or close_signal is None):
                continue
            replacement = self._create_start_finalizer(
                start_task, runner, retire_signal, terminal_signal,
                close_signal)
            self._track_background_task(replacement, expect_true=True)

    def _mark_start_detached(
            self, runner: BaseRunner,
            close_timeout: float | None = None) -> bool:
        start_task = self._runner_startups.get(runner)
        if start_task is None:
            return False
        if close_timeout is not None:
            self._startup_close_timeouts[start_task] = min(
                _RUNNER_CONTROL_TIMEOUT, max(close_timeout, 0.0))
        if start_task in self._detached_startups:
            return True
        retire_signal = self._startup_retire_signals.get(start_task)
        finalizer = self._startup_finalizers.get(start_task)
        if retire_signal is None or finalizer is None:
            return False
        self._detached_startups.add(start_task)
        self._track_background_task(finalizer, expect_true=True)
        retire_signal.set()
        return True

    async def _wait_start_close_attempt(self, runner: BaseRunner) -> None:
        start_task = self._runner_startups.get(runner)
        if start_task is None:
            return
        close_signal = self._startup_close_signals.get(start_task)
        if close_signal is not None:
            await close_signal.wait()

    def _check_shutdown_epoch(self, epoch: int, spec: AgentSpec) -> None:
        if self._shutdown_active or epoch != self._shutdown_epoch:
            raise _PoolShutdownInterrupted(
                f"{spec.label}: pool shutdown interrupted queued request")

    def _check_cancel_epoch(self, epoch: int) -> None:
        if self._cancel_active or epoch != self._cancel_epoch:
            raise asyncio.CancelledError

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

    def _discard_owned_start_lifecycle(self, runner: BaseRunner) -> None:
        """Replace a completed, still-owned start generation without closing."""
        start_task = self._runner_startups.get(runner)
        if start_task is None:
            return
        if not start_task.done() or start_task in self._detached_startups:
            raise RuntimeError(
                f"{runner.spec.label}: cannot replace active start lifecycle")
        finalizer = self._startup_finalizers.get(start_task)
        if finalizer is None:
            return
        self._forget_start_lifecycle(start_task, finalizer)
        _cancel_and_drain(finalizer)

    async def _start_runner(
            self, spec: AgentSpec, runner: BaseRunner) -> None:
        self._discard_owned_start_lifecycle(runner)
        retire_signal = asyncio.Event()
        terminal_signal = asyncio.Event()
        close_signal = asyncio.Event()
        start_task = asyncio.create_task(
            runner.start(self._workdir),
            name=f"leftover-start-{spec.key}",
        )
        self._startup_tasks.add(start_task)
        self._startup_runners[start_task] = runner
        self._startup_retire_signals[start_task] = retire_signal
        self._startup_terminal_signals[start_task] = terminal_signal
        self._startup_close_signals[start_task] = close_signal
        self._startup_close_timeouts[start_task] = min(
            _RUNNER_CONTROL_TIMEOUT,
            max(POOL_TRANSITION_TIMEOUT / 2, 0.0),
        )
        self._startup_lifecycle_runners[start_task] = runner
        self._runner_startups[runner] = start_task
        self._create_start_finalizer(
            start_task, runner, retire_signal, terminal_signal, close_signal)

        def startup_finished(done: asyncio.Task[Any]) -> None:
            self._startup_tasks.discard(done)
            self._startup_runners.pop(done, None)
            terminal_signal.set()
            _consume_task_result(done)

        start_task.add_done_callback(startup_finished)
        try:
            await _await_hard(start_task, START_TIMEOUT)
        except TimeoutError:
            raise TimeoutError(
                f"{spec.label}: runner start timed out after {START_TIMEOUT:g}s"
            ) from None

    async def _get_unlocked(self, spec: AgentSpec) -> BaseRunner:
        runner = self._runners.get(spec.key)
        if runner is not None:
            from .acp_runner import AcpRunner
            if isinstance(runner, AcpRunner) and not runner.live_session():
                await self._start_runner(spec, runner)
            return runner
        runner = build_runner(spec)
        self._runners[spec.key] = runner
        await self._start_runner(spec, runner)
        return runner

    async def get(self, spec: AgentSpec) -> BaseRunner:
        shutdown_epoch = self._shutdown_epoch
        cancel_epoch = self._cancel_epoch
        async with self._warmup_owner():
            async with self._runner_slot(spec):
                async with self._operations.read():
                    self._check_shutdown_epoch(shutdown_epoch, spec)
                    self._check_cancel_epoch(cancel_epoch)
                    initial = self._runners.get(spec.key)
                    try:
                        runner = await self._get_unlocked(spec)
                        self._check_shutdown_epoch(shutdown_epoch, spec)
                        self._check_cancel_epoch(cancel_epoch)
                    except BaseException:
                        self._rollback_warmup_runner(spec, initial)
                        raise
                    return runner

    async def _prepare_unlocked(self, spec: AgentSpec) -> BaseRunner:
        """Start one managed runner, installing its exec fallback on failure."""
        try:
            return await self._get_unlocked(spec)
        except Exception as exc:              # noqa: BLE001
            log.warning("%s: ACP start failed (%s); falling back to exec",
                        spec.label, exc)
            failed = self._runners.pop(spec.key, None)
            if failed is not None:
                if self._mark_start_detached(failed):
                    await self._wait_start_close_attempt(failed)
            fallback = ExecRunner(spec)
            try:
                await self._start_runner(spec, fallback)
            except BaseException:
                self._mark_start_detached(fallback)
                raise
            self._runners[spec.key] = fallback
            self._fallback_errors[spec.key] = str(exc)
            return fallback

    async def prepare(self, spec: AgentSpec) -> BaseRunner:
        """Single-flight warmup that leaves a runnable ACP or exec backend."""
        shutdown_epoch = self._shutdown_epoch
        cancel_epoch = self._cancel_epoch
        async with self._warmup_owner():
            async with self._runner_slot(spec):
                async with self._operations.read():
                    self._check_shutdown_epoch(shutdown_epoch, spec)
                    self._check_cancel_epoch(cancel_epoch)
                    initial = self._runners.get(spec.key)
                    try:
                        runner = await self._prepare_unlocked(spec)
                        self._check_shutdown_epoch(shutdown_epoch, spec)
                        self._check_cancel_epoch(cancel_epoch)
                    except BaseException:
                        self._rollback_warmup_runner(spec, initial)
                        raise
                    return runner

    def submit(
            self, spec: AgentSpec, prompt: str,
            on_event: OnEvent | None = None, *,
            parent_id: str | None = None,
            publish_completion: bool = True) -> TurnHandle:
        """Submit a turn and return its handle before execution can begin.

        Published completions enter a bounded, process-local FIFO consumed by
        :meth:`next_completion`. This queue is a callback channel, not durable
        task storage. Compatibility calls through :meth:`run` do not publish.
        """
        loop = asyncio.get_running_loop()
        callback = self._publish_completion if publish_completion else None
        handle = TurnHandle(
            spec, parent_id=parent_id, on_settled=callback)
        self._active_turns[handle.turn_id] = handle
        shutdown_epoch = self._shutdown_epoch
        cancel_epoch = self._cancel_epoch
        task = loop.create_task(
            self._execute_handle(
                handle, spec, prompt, on_event,
                shutdown_epoch, cancel_epoch),
            name=f"leftover-turn-{spec.key}-{handle.turn_id[:8]}",
        )
        handle._bind_task(task)
        task.add_done_callback(
            lambda done: self._handle_worker_done(handle, done))
        return handle

    async def next_completion(
            self, timeout: float | None = None, *,
            parent_id: str | None = None) -> TurnHandle:
        """Return the next published completion without cancelling its task."""
        inbox = self._completion_inboxes.setdefault(
            parent_id, asyncio.Queue(maxsize=COMPLETION_INBOX_SIZE))
        self._completion_waiters[parent_id] = (
            self._completion_waiters.get(parent_id, 0) + 1)
        try:
            if timeout is not None and timeout <= 0:
                try:
                    handle = inbox.get_nowait()
                except asyncio.QueueEmpty:
                    raise TimeoutError from None
            else:
                getter = inbox.get()
                if timeout is None:
                    handle = await getter
                else:
                    handle = await asyncio.wait_for(getter, timeout)
            inbox.task_done()
            return handle
        finally:
            waiters = self._completion_waiters[parent_id] - 1
            if waiters:
                self._completion_waiters[parent_id] = waiters
            else:
                self._completion_waiters.pop(parent_id, None)
                if (parent_id is not None and inbox.empty()
                        and self._completion_inboxes.get(parent_id) is inbox):
                    self._completion_inboxes.pop(parent_id, None)

    def completion_overflows(self, *, parent_id: str | None = None) -> int:
        """Number of oldest completions dropped for one callback owner."""
        return self._completion_overflows.get(parent_id, 0)

    def discard_completions(self, *, parent_id: str | None = None) -> int:
        """Discard one owner's unconsumed callbacks and release its inbox."""
        if self._completion_waiters.get(parent_id, 0):
            raise RuntimeError("cannot discard a completion inbox with waiters")
        inbox = self._completion_inboxes.get(parent_id)
        discarded = 0
        if inbox is not None:
            while True:
                try:
                    inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                inbox.task_done()
                discarded += 1
            if parent_id is not None:
                self._completion_inboxes.pop(parent_id, None)
        self._completion_overflows.pop(parent_id, None)
        return discarded

    def _publish_completion(self, handle: TurnHandle) -> None:
        inbox = self._completion_inboxes.setdefault(
            handle.parent_id, asyncio.Queue(maxsize=COMPLETION_INBOX_SIZE))
        if inbox.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                inbox.get_nowait()
                inbox.task_done()
                self._completion_overflows[handle.parent_id] = (
                    self._completion_overflows.get(handle.parent_id, 0) + 1)
        inbox.put_nowait(handle)

    def _handle_worker_done(
            self, handle: TurnHandle, task: asyncio.Task[None]) -> None:
        if not handle.done():
            if task.cancelled():
                handle._settle_cancelled()
            else:
                exc = task.exception()
                if exc is None:
                    exc = RuntimeError("turn worker exited without settlement")
                handle._settle_exception(exc)
        handle._mark_cleanup_done()
        self._active_turns.pop(handle.turn_id, None)
        _consume_task_result(task)

    async def _execute_handle(
            self, handle: TurnHandle, spec: AgentSpec, prompt: str,
            on_event: OnEvent | None, shutdown_epoch: int,
            cancel_epoch: int) -> None:
        try:
            self._check_shutdown_epoch(shutdown_epoch, spec)
            self._check_cancel_epoch(cancel_epoch)
            async with self._runner_slot(spec):
                async with self._operations.read():
                    self._check_shutdown_epoch(shutdown_epoch, spec)
                    self._check_cancel_epoch(cancel_epoch)
                    initial = self._runners.get(spec.key)
                    try:
                        runner = await self._prepare_unlocked(spec)
                        self._check_shutdown_epoch(shutdown_epoch, spec)
                        self._check_cancel_epoch(cancel_epoch)
                    except BaseException:
                        self._rollback_warmup_runner(spec, initial)
                        raise
                    if not handle._mark_running(float(spec.timeout)):
                        raise asyncio.CancelledError
                    settler = handle._settle_result
                    settler_token = runner._bind_turn_settler(settler)
                    try:
                        turn = await runner.run(prompt, on_event)
                        fallback_error = self._fallback_errors.pop(
                            spec.key, None)
                        if fallback_error and not turn.ok and turn.error:
                            turn.error = (
                                f"acp start failed ({fallback_error}); "
                                f"exec fallback: {turn.error}")
                        handle._settle_result(turn)
                    finally:
                        runner._unbind_turn_settler(settler_token)
                        try:
                            await runner.wait_cleanup()
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:  # noqa: BLE001
                            log.warning("%s: runner cleanup wait failed: %s",
                                        spec.label, exc)
        except (_RunnerQueueTimeout, _PoolShutdownInterrupted) as exc:
            meta = {"not_executed": True}
            if isinstance(exc, _RunnerQueueTimeout):
                meta["queue_timeout"] = True
            else:
                meta["shutdown_interrupted"] = True
            handle._settle_result(Turn(
                agent=spec,
                error=f"not executed: {exc}",
                seconds=time.monotonic() - handle.created_at,
                meta=meta,
            ))
        except asyncio.CancelledError:
            handle._settle_cancelled()
            raise
        except Exception as exc:  # noqa: BLE001 - preserve the runner failure
            handle._settle_exception(exc)

    async def run(self, spec: AgentSpec, prompt: str,
                  on_event: OnEvent | None = None) -> Turn:
        handle = self.submit(
            spec, prompt, on_event, publish_completion=False)
        try:
            return await handle.wait()
        except asyncio.CancelledError:
            handle.cancel()
            raise

    async def cancel_all(self) -> None:
        timeout = POOL_TRANSITION_TIMEOUT
        await self._cancel_all_unlocked(
            time.monotonic() + timeout, "cancel", timeout)

    async def _cancel_all_unlocked(
            self, deadline: float | None = None, operation: str = "cancel",
            timeout: float = POOL_TRANSITION_TIMEOUT) -> bool:
        if deadline is None:
            deadline = time.monotonic() + timeout
        acquired = False
        workers: list[TurnHandle] = []
        startup_tasks: list[asyncio.Task[Any]] = []
        warmup_owners: list[asyncio.Task[Any]] = []
        cancelled_cleanly = True
        try:
            await _acquire_before(
                self._cancel_lock, deadline, operation, timeout)
            acquired = True
            self._cancel_epoch += 1
            self._cancel_active = True
            startup_tasks = [
                task for task in self._startup_tasks if not task.done()
            ]
            warmup_owners = [
                task for task in self._warmup_owners if not task.done()
            ]
            for task in startup_tasks:
                _request_task_cancel(task)
            handles = list(self._active_turns.values())
            workers = [
                handle for handle in handles if not handle.cleanup_done()
            ]
            for handle in handles:
                if handle.done():
                    continue
                queued = handle.state is TurnState.QUEUED
                if operation == "shutdown":
                    detail = ("not executed: pool shutdown interrupted queued "
                              "request" if queued else
                              "cancelled: pool shutdown interrupted running "
                              "request")
                    meta = {"shutdown_interrupted": True}
                    if queued:
                        meta["not_executed"] = True
                    handle._settle_cancelled(error=detail, meta=meta)
                else:
                    handle._settle_cancelled()
                if queued:
                    worker = handle._task
                    if worker is not None:
                        _request_task_cancel(worker)

            current = asyncio.current_task()
            for task in warmup_owners:
                if task is not current:
                    _request_task_cancel(task)

            # Publish cancellation before the graceful runner interrupt. Any
            # runner that does not stop within the control bound then receives
            # direct worker cancellation below.
            remaining = _remaining(deadline, operation, timeout)
            results = await asyncio.gather(*(
                _cancel_runner(
                    runner, remaining,
                    track_detached=self._track_background_task)
                for runner in list(self._runners.values())),
                return_exceptions=True)
            cancelled_cleanly = all(result is True for result in results)
        finally:
            if acquired:
                # Do this even when the graceful interrupt or its caller is
                # cancelled, otherwise a settled turn can retain its runner
                # slot indefinitely without an owner left to stop it.
                for handle in workers:
                    worker = handle._task
                    if worker is not None:
                        _request_task_cancel(worker)
                for task in startup_tasks:
                    _request_task_cancel(task)
                for task in warmup_owners:
                    if task is not asyncio.current_task():
                        _request_task_cancel(task)
                self._cancel_active = False
                self._cancel_epoch += 1
                self._cancel_lock.release()
        return cancelled_cleanly

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
                cancelled_cleanly = await self._cancel_all_unlocked(
                    deadline, "shutdown", timeout)
                await _acquire_before(
                    self._lock, deadline, "shutdown", timeout)
                transition_acquired = True
                async with self._operations.write(
                        deadline=deadline, operation="shutdown",
                        timeout=timeout):
                    await self._shutdown_unlocked(
                        deadline, "shutdown", timeout)
                    if not cancelled_cleanly:
                        raise _transition_timeout("shutdown", timeout)
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
            # A failed close remains a lifecycle owner. Only a later explicit
            # transition creates its next attempt.
            self._retry_failed_start_finalizers()
            runners = list(self._runners.values())
            close_timeout = max(deadline - time.monotonic(), 0.0) / 2
            for runner in runners:
                if not self._mark_start_detached(
                        runner, close_timeout=close_timeout):
                    self._close_detached_runner(runner)
            self._runners.clear()
            self._fallback_errors.clear()
            await self._drain_background_tasks(
                deadline, operation, timeout)
        finally:
            if acquired:
                self._cancel_lock.release()
