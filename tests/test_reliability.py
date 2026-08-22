"""Reliability checks for quota probes and the cross-process ledger."""
from __future__ import annotations

import asyncio
import contextlib
import json
import multiprocessing
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leftover import quota as q  # noqa: E402
from leftover.agents import exec_runner as exec_mod  # noqa: E402
from leftover.agents.exec_runner import ExecRunner  # noqa: E402
from leftover.config import AgentSpec, Config, Routing  # noqa: E402
from leftover.router import Router  # noqa: E402


class _NoopPool:
    def peek(self, spec: AgentSpec):
        return None


def _record_worker(path: str, worker: int, count: int, ready, start) -> None:
    ledger = q.Ledger(path)
    ready.put(worker)
    if not start.wait(10):
        raise RuntimeError("ledger test start barrier timed out")
    for offset in range(count):
        ledger.record("grok", worker * 1000 + offset, True)


def test_ledger_concurrent_records_are_not_lost() -> None:
    workers = 4
    records_per_worker = 12
    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.json"
        q.Ledger(path).record("claude", 1.0, True)
        ready = ctx.Queue()
        start = ctx.Event()
        processes = [
            ctx.Process(
                target=_record_worker,
                args=(str(path), worker, records_per_worker, ready, start),
            )
            for worker in range(workers)
        ]
        try:
            for process in processes:
                process.start()
            for _ in processes:
                ready.get(timeout=10)
            start.set()
            for process in processes:
                process.join(timeout=20)
            assert all(not process.is_alive() for process in processes)
            assert [process.exitcode for process in processes] == [0] * workers
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            ready.close()
            ready.join_thread()

        raw = json.loads(path.read_text())
        rows = raw["events"]["grok"]
        assert len(rows) == workers * records_per_worker
        assert len({row[1] for row in rows}) == workers * records_per_worker
        assert len(raw["events"]["claude"]) == 1
        assert q.Ledger(path).count("grok", 3600) == workers * records_per_worker
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_ledger_recovers_from_empty_or_damaged_files() -> None:
    damaged = ("", "{broken", "[]", '{"events": null}',
               '{"events": {"grok": [["bad"]]}}')
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.json"
        for payload in damaged:
            path.write_text(payload)
            ledger = q.Ledger(path)
            assert ledger.events == {}
            ledger.record("grok", 2.5, True)
            written = json.loads(path.read_text())
            assert len(written["events"]["grok"]) == 1
            assert q.Ledger(path).count("grok", 3600) == 1


def test_stale_ledger_snapshots_merge_before_writing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "ledger.json"
        first = q.Ledger(path)
        stale = q.Ledger(path)
        first.record("gpt", 3.0, True)
        stale.record("grok", 4.0, False)
        loaded = q.Ledger(path).events
        assert len(loaded["gpt"]) == 1
        assert len(loaded["grok"]) == 1


def test_grok_acp_probe_has_a_hard_timeout() -> None:
    class HangingConnection:
        def __init__(self) -> None:
            self.called = False
            self.cancelled = False

        async def ext_method(self, method: str, params: dict) -> dict:
            self.called = method == "x.ai/billing" and params == {}
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                # A plain asyncio.wait_for() waits for this cleanup and misses
                # its advertised deadline. The probe must detach it instead.
                await asyncio.sleep(0.4)
            return {}

    async def run() -> None:
        conn = HangingConnection()
        started = time.monotonic()
        found = await q.probe_grok_acp(conn, timeout=0.05)
        elapsed = time.monotonic() - started
        await asyncio.sleep(0)
        assert q.GROK_ACP_PROBE_TIMEOUT == 3.0
        assert found is None
        assert conn.called and conn.cancelled
        assert elapsed < 0.2

    asyncio.run(run())


def test_grok_acp_probe_still_parses_a_fast_reply() -> None:
    class FastConnection:
        async def ext_method(self, method: str, params: dict) -> dict:
            assert method == "x.ai/billing" and params == {}
            return {
                "monthlyLimit": {"val": 100},
                "usage": {"totalUsed": {"val": 25}},
                "billingCycle": {"billingPeriodEnd": "2026-09-01T00:00:00Z"},
            }

    found = asyncio.run(q.probe_grok_acp(FastConnection(), timeout=0.1))
    assert found is not None
    assert len(found.windows) == 1
    assert found.windows[0].used_percent == 25.0


def test_sync_quota_timeout_does_not_delay_asyncio_run_shutdown() -> None:
    release = threading.Event()
    started = threading.Event()

    def slow_probe() -> q.Quota | None:
        started.set()
        release.wait(0.6)
        return None

    with tempfile.TemporaryDirectory() as tmp:
        spec = AgentSpec(
            key="claude", label="Claude", quota_probe="claude")
        cfg = Config(
            agents=[spec], data_dir=tmp,
            routing=Routing(quota_probe_timeout=0.05),
        )
        router = Router(cfg, _NoopPool())
        cached = q.Quota(agent="claude", windows=[q.Window(
            name="weekly", used_percent=42.0,
            resets_at=time.time() + 3600, source=q.REPORTED,
        )])
        router.h(spec).quota = cached
        router.h(spec).quota_checked = 0.0

        with patch.object(q, "probe_claude", slow_probe):
            began = time.monotonic()
            try:
                found = asyncio.run(router.quota_for(spec, force=True))
            finally:
                release.set()
            elapsed = time.monotonic() - began
        with patch.object(
                q, "probe_claude",
                return_value=q.Quota(agent="claude", note="no live number")):
            empty_refresh = asyncio.run(router.quota_for(spec, force=True))

    assert Routing().quota_probe_timeout == 20.0
    assert started.is_set()
    assert elapsed < 0.25
    assert any(window.source == q.REPORTED for window in found.windows)
    assert found.windows[0].used_percent == 42.0
    assert empty_refresh.windows[0].used_percent == 42.0


def test_cancelled_sync_quota_probe_does_not_hold_event_loop_open() -> None:
    release = threading.Event()
    started = threading.Event()

    def slow_probe() -> q.Quota | None:
        started.set()
        release.wait(0.6)
        return None

    async def cancel_probe(router: Router, spec: AgentSpec) -> None:
        task = asyncio.create_task(router.quota_for(spec, force=True))
        for _ in range(200):
            if started.is_set():
                break
            await asyncio.sleep(0.001)
        assert started.is_set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("quota probe cancellation was swallowed")

    with tempfile.TemporaryDirectory() as tmp:
        spec = AgentSpec(
            key="claude", label="Claude", quota_probe="claude")
        cfg = Config(
            agents=[spec], data_dir=tmp,
            routing=Routing(quota_probe_timeout=1.0),
        )
        router = Router(cfg, _NoopPool())
        with patch.object(q, "probe_claude", slow_probe):
            began = time.monotonic()
            try:
                asyncio.run(cancel_probe(router, spec))
            finally:
                release.set()
            elapsed = time.monotonic() - began

    assert elapsed < 0.25


def test_grok_probe_phases_share_one_total_budget() -> None:
    release = threading.Event()
    acp_timeouts: list[float] = []
    rest_timeouts: list[float | None] = []
    local_calls: list[bool] = []

    async def slow_acp(conn, timeout: float) -> q.Quota | None:
        acp_timeouts.append(timeout)
        await asyncio.sleep(0.03)
        return None

    def slow_rest(home=None, timeout: float | None = None) -> q.Quota | None:
        rest_timeouts.append(timeout)
        release.wait(0.6)
        return None

    def local_probe(home=None) -> q.Quota | None:
        local_calls.append(True)
        return None

    class GrokPool:
        class Runner:
            _conn = object()

        def peek(self, spec: AgentSpec):
            return self.Runner()

    with tempfile.TemporaryDirectory() as tmp:
        spec = AgentSpec(key="grok", label="Grok", quota_probe="grok")
        cfg = Config(
            agents=[spec], data_dir=tmp,
            routing=Routing(quota_probe_timeout=0.05),
        )
        router = Router(cfg, GrokPool())
        with (patch.object(q, "probe_grok_acp", slow_acp),
              patch.object(q, "probe_grok_rest", slow_rest),
              patch.object(q, "probe_grok_local", local_probe)):
            began = time.monotonic()
            try:
                asyncio.run(router.quota_for(spec, force=True))
            finally:
                release.set()
            elapsed = time.monotonic() - began

    assert elapsed < 0.25
    assert len(acp_timeouts) == 1 and 0 < acp_timeouts[0] <= 0.05
    assert len(rest_timeouts) == 1
    assert rest_timeouts[0] is not None and rest_timeouts[0] < 0.05
    assert not local_calls


def test_sub2api_pagination_honors_one_deadline() -> None:
    calls: list[float] = []

    def page(root: str, key: str, path: str,
             timeout: float = 12.0) -> tuple[int, dict]:
        calls.append(timeout)
        time.sleep(min(0.02, timeout))
        return 200, {"data": {
            "items": [{"id": len(calls)}],
            "pages": 20,
        }}

    began = time.monotonic()
    with patch.object(q, "_sub2api_get", page):
        items = q._sub2api_list_accounts(
            "https://example.invalid", "secret",
            deadline=time.monotonic() + 0.055,
        )
    elapsed = time.monotonic() - began

    assert items
    assert 1 <= len(calls) < 20
    assert elapsed < 0.15
    assert calls[-1] <= calls[0]


def test_quota_report_refreshes_agents_in_parallel() -> None:
    active = 0
    max_active = 0

    async def slow_quota(self: Router, spec: AgentSpec,
                         force: bool = False) -> q.Quota:
        nonlocal active, max_active
        assert force
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.08)
        finally:
            active -= 1
        return q.Quota(agent=spec.key)

    agents = [
        AgentSpec(key=key, label=key.title())
        for key in ("claude", "gpt", "grok", "cursor")
    ]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(agents=agents, data_dir=tmp)
        router = Router(cfg, _NoopPool())
        with patch.object(Router, "quota_for", slow_quota):
            began = time.monotonic()
            report = asyncio.run(router.report())
            elapsed = time.monotonic() - began

    assert max_active == len(agents)
    assert elapsed < 0.2
    assert all(spec.label in report for spec in agents)


def test_stream_json_eof_cannot_bypass_turn_timeout() -> None:
    async def run() -> None:
        script = (
            "import os, time\n"
            "os.close(1)\n"
            "time.sleep(60)\n"
        )
        spec = AgentSpec(
            key="eof-hang",
            label="EOF hang",
            transport="exec",
            exec_command=[sys.executable, "-c", script],
            exec_output="stream-json",
            timeout=0.05,
        )
        runner = ExecRunner(spec)
        await runner.start(str(ROOT))
        started = time.monotonic()
        turn = await runner.run("prompt")
        elapsed = time.monotonic() - started
        assert turn.error == "timed out after 0.05s"
        assert turn.meta.get("timeout_kind") == "turn"
        assert elapsed < 0.3
        assert runner._proc is None

    asyncio.run(run())


async def _run_exec_with_watchdog(
        runner: ExecRunner, prompt: str, timeout: float = 1.5):
    """Keep a pipe regression from hanging the standalone test process."""
    task = asyncio.create_task(runner.run(prompt))
    done, _pending = await asyncio.wait({task}, timeout=timeout)
    if task in done:
        return task.result()

    proc = runner._proc
    drains = []
    if proc is not None:
        drains = [
            asyncio.create_task(stream.read())
            for stream in (proc.stdout, proc.stderr)
            if stream is not None
        ]
    if proc is not None and proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    if drains:
        await asyncio.gather(*drains, return_exceptions=True)
    task.cancel()
    done, _pending = await asyncio.wait({task}, timeout=1.0)
    if task in done:
        await asyncio.gather(task, return_exceptions=True)
    raise AssertionError(f"exec turn exceeded {timeout}s watchdog")


def test_stream_json_drains_large_stderr_until_timeout() -> None:
    async def run() -> None:
        reader = asyncio.StreamReader()
        marker = b"bounded-tail-marker"
        reader.feed_data(b"x" * (2 * 1024 * 1024) + marker)
        reader.feed_eof()
        captured = await exec_mod._drain_bounded_tail(reader)
        assert len(captured) == exec_mod._STDERR_CAPTURE_LIMIT
        assert captured.endswith(marker)

        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "stderr-ready"
            script = (
                "import os, pathlib, sys, time\n"
                "chunk = b'x' * 65536\n"
                "for _ in range(32):\n"
                "    os.write(2, chunk)\n"
                "pathlib.Path(sys.argv[1]).write_text('ready')\n"
                "time.sleep(60)\n"
            )
            spec = AgentSpec(
                key="stderr-hang",
                label="Stderr hang",
                transport="exec",
                exec_command=[sys.executable, "-c", script, str(ready)],
                exec_output="stream-json",
                timeout=0.3,
            )
            runner = ExecRunner(spec)
            await runner.start(tmp)
            started = time.monotonic()
            turn = await _run_exec_with_watchdog(runner, "prompt")
            elapsed = time.monotonic() - started
            assert ready.exists()
            assert turn.error == "timed out after 0.3s"
            assert turn.meta.get("timeout_kind") == "turn"
            assert elapsed < 0.8
            assert runner._proc is None

    asyncio.run(run())


def test_stream_json_large_stdin_honors_turn_timeout() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ready = Path(tmp) / "stdin-ready"
            script = (
                "import pathlib, sys, time\n"
                "pathlib.Path(sys.argv[1]).write_text('ready')\n"
                "time.sleep(60)\n"
            )
            spec = AgentSpec(
                key="stdin-hang",
                label="Stdin hang",
                transport="exec",
                exec_command=[sys.executable, "-c", script, str(ready)],
                exec_input="stdin",
                exec_output="stream-json",
                timeout=0.3,
            )
            runner = ExecRunner(spec)
            await runner.start(tmp)
            started = time.monotonic()
            turn = await _run_exec_with_watchdog(
                runner, "x" * (4 * 1024 * 1024))
            elapsed = time.monotonic() - started
            assert ready.exists()
            assert turn.error == "timed out after 0.3s"
            assert turn.meta.get("timeout_kind") == "turn"
            assert elapsed < 0.8
            assert runner._proc is None

    asyncio.run(run())


def test_stream_json_overlong_stdout_cannot_deadlock_reap() -> None:
    async def run() -> None:
        script = (
            "import os, time\n"
            "chunk = b'x' * 65536\n"
            "for _ in range(32):\n"
            "    os.write(1, chunk)\n"
            "time.sleep(60)\n"
        )
        spec = AgentSpec(
            key="stdout-hang",
            label="Stdout hang",
            transport="exec",
            exec_command=[sys.executable, "-c", script],
            exec_output="stream-json",
            timeout=0.3,
        )
        runner = ExecRunner(spec)
        await runner.start(str(ROOT))
        started = time.monotonic()
        turn = await _run_exec_with_watchdog(runner, "prompt")
        elapsed = time.monotonic() - started
        assert not turn.ok
        assert elapsed < 0.8
        assert runner._proc is None

    asyncio.run(run())


def test_exec_metadata_is_scoped_to_one_turn() -> None:
    async def run() -> None:
        script = (
            "import json, sys\n"
            "if sys.argv[-1] == 'first':\n"
            "    print(json.dumps({'type': 'error', 'is_error': True, "
            "'message': 'first failed'}))\n"
            "else:\n"
            "    print('SECOND OK')\n"
        )
        spec = AgentSpec(
            key="metadata",
            label="Metadata",
            transport="exec",
            exec_command=[sys.executable, "-c", script],
            exec_output="json",
            timeout=1,
        )
        runner = ExecRunner(spec)
        await runner.start(str(ROOT))
        first = await runner.run("first")
        second = await runner.run("second")
        assert first.error == "first failed"
        assert first.meta.get("is_error") is True
        assert second.ok and second.text == "SECOND OK"
        assert second.meta == {}

    asyncio.run(run())


def main() -> int:
    tests = [
        test_ledger_concurrent_records_are_not_lost,
        test_ledger_recovers_from_empty_or_damaged_files,
        test_stale_ledger_snapshots_merge_before_writing,
        test_grok_acp_probe_has_a_hard_timeout,
        test_grok_acp_probe_still_parses_a_fast_reply,
        test_sync_quota_timeout_does_not_delay_asyncio_run_shutdown,
        test_cancelled_sync_quota_probe_does_not_hold_event_loop_open,
        test_grok_probe_phases_share_one_total_budget,
        test_sub2api_pagination_honors_one_deadline,
        test_quota_report_refreshes_agents_in_parallel,
        test_stream_json_eof_cannot_bypass_turn_timeout,
        test_stream_json_drains_large_stderr_until_timeout,
        test_stream_json_large_stdin_honors_turn_timeout,
        test_stream_json_overlong_stdout_cannot_deadlock_reap,
        test_exec_metadata_is_scoped_to_one_turn,
    ]
    failed = 0
    for test in tests:
        try:
            test()
        except Exception as exc:  # noqa: BLE001 - standalone test runner
            failed += 1
            print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}")
        else:
            print(f"PASS {test.__name__}")
    print(f"{len(tests) - failed}/{len(tests)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
