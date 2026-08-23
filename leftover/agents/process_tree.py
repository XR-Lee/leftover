"""Bounded lifecycle management for subprocess process groups."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from dataclasses import dataclass
from typing import Any, Protocol


_POLL_INTERVAL = 0.01


class AsyncProcess(Protocol):
    """The subset shared by asyncio subprocess-like process handles."""

    pid: int
    returncode: int | None

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


def isolated_subprocess_kwargs() -> dict[str, Any]:
    """Return subprocess options that isolate a POSIX subprocess tree."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {}


@dataclass(frozen=True, slots=True)
class ProcessTree:
    """A process handle plus the stable group created for its descendants."""

    process: AsyncProcess
    process_group: int | None

    @classmethod
    def capture(cls, process: AsyncProcess, *, isolated: bool) -> ProcessTree:
        group: int | None = None
        pid = getattr(process, "pid", None)
        if os.name == "posix" and isolated and isinstance(pid, int) and pid > 0:
            # start_new_session=True makes the child both session leader and
            # process-group leader, so its PID remains the group identifier even
            # after the leader itself has exited.
            if pid != os.getpgrp():
                group = pid
        return cls(process=process, process_group=group)


def _consume_future(future: asyncio.Future[Any]) -> None:
    with contextlib.suppress(BaseException):
        future.exception()


def _group_exists(group: int | None) -> bool:
    if group is None or group <= 0 or group == os.getpgrp():
        return False
    try:
        os.killpg(group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_tree(
        tree: ProcessTree, sig: signal.Signals, *, force: bool = False) -> None:
    group = tree.process_group
    if group is not None and group > 0 and group != os.getpgrp():
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            # A setsid wrapper can publish its leader handle just before the
            # new group becomes observable. Fall through to the leader so this
            # startup race still receives the requested signal.
            pass
        else:
            return

    process = tree.process
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        if force:
            process.kill()
        else:
            process.terminate()


async def _wait_for_tree_exit(
        tree: ProcessTree, waiter: asyncio.Task[int], timeout: float) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(timeout, 0.0)
    while True:
        leader_exited = tree.process.returncode is not None or waiter.done()
        group_exited = not _group_exists(tree.process_group)
        if leader_exited and group_exited:
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.sleep(min(_POLL_INTERVAL, remaining))


async def terminate_process_tree(
        tree: ProcessTree, *, term_timeout: float, kill_timeout: float) -> None:
    """Terminate a whole subprocess group without an unbounded wait.

    The saved process-group identifier is signalled even when the group leader
    has already exited, because descendants may still be alive and retaining
    inherited pipes.
    """
    waiter = asyncio.create_task(tree.process.wait())
    try:
        _signal_tree(tree, signal.SIGTERM)
        if await _wait_for_tree_exit(tree, waiter, term_timeout):
            return

        kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
        _signal_tree(tree, kill_signal, force=True)
        await _wait_for_tree_exit(tree, waiter, kill_timeout)
    finally:
        if not waiter.done():
            waiter.cancel()
            waiter.add_done_callback(_consume_future)
        else:
            _consume_future(waiter)
