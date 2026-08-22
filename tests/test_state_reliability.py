"""Cross-process reliability checks for macbot-state.json."""
from __future__ import annotations

import json
import multiprocessing
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from leftover.config import AgentSpec, Config  # noqa: E402
from leftover.macbot import (  # noqa: E402
    STATE_NAME, load_state, persist_health, restore_health, save_state)
from leftover.quota import Quota, Window, REPORTED  # noqa: E402
from leftover.router import Router, State  # noqa: E402


class _NoopPool:
    def peek(self, _spec):
        return None


def _agents(count: int) -> list[AgentSpec]:
    return [AgentSpec(key=f"agent-{index}", label=f"Agent {index}")
            for index in range(count)]


def _concurrent_writer(data_dir: str, index: int, count: int,
                       ready, start, result) -> None:
    try:
        cfg = Config(agents=_agents(count), data_dir=data_dir)
        router = Router(cfg, _NoopPool())
        state = load_state(cfg)
        restore_health(router, state)
        ready.put(index)
        if not start.wait(10):
            raise TimeoutError("writer start barrier timed out")

        spec = cfg.agents[index]
        health = router.h(spec)
        health.state = State.COOLING
        health.until = 2_000_000_000.0 + index
        health.last_error = f"failure-{index}"
        health.consecutive = index + 1
        health.quota = Quota(
            agent=spec.key,
            checked_at=1_900_000_000.0 + index,
            windows=[Window(
                name="weekly", used_percent=float(index + 1),
                resets_at=2_000_000_000.0, source=REPORTED)],
        )
        health.quota_checked = health.quota.checked_at
        state.setdefault("sticky", {})[f"/workspace/{index}"] = spec.key

        # Repeated commits make it likely that a reader overlaps a replacement,
        # while every iteration still changes only this process's agent entry.
        for attempt in range(12):
            health.consecutive = index + attempt + 1
            health.quota.checked_at += 0.001
            health.quota_checked = health.quota.checked_at
            persist_health(cfg, router, state)
            time.sleep(0.001)
        result.put((index, ""))
    except Exception as exc:  # noqa: BLE001 - child errors must reach the parent
        result.put((index, f"{type(exc).__name__}: {exc}"))


class StateReliabilityTests(unittest.TestCase):
    def test_concurrent_stale_snapshots_merge_and_stay_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            count = 8
            cfg = Config(agents=_agents(count), data_dir=tmp)
            self.assertTrue(save_state(cfg, {
                "sticky": {"/seed": "agent-0"},
                "health": {},
                "quota": {},
            }))

            ctx = multiprocessing.get_context("spawn")
            ready = ctx.Queue()
            result = ctx.Queue()
            start = ctx.Event()
            processes = [ctx.Process(
                target=_concurrent_writer,
                args=(tmp, index, count, ready, start, result),
            ) for index in range(count)]
            for process in processes:
                process.start()
            for _ in processes:
                ready.get(timeout=20)
            start.set()

            state_path = Path(tmp) / STATE_NAME
            decode_errors: list[str] = []
            while any(process.is_alive() for process in processes):
                try:
                    json.loads(state_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    decode_errors.append(str(exc))
                time.sleep(0.0005)

            for process in processes:
                process.join(timeout=5)
                self.assertEqual(process.exitcode, 0)
            child_errors = [result.get(timeout=5)[1] for _ in processes]
            self.assertFalse([error for error in child_errors if error])
            self.assertFalse(decode_errors)

            raw = json.loads(state_path.read_text(encoding="utf-8"))
            state = load_state(cfg)
            self.assertEqual(raw, state)
            self.assertEqual(state["sticky"]["/seed"], "agent-0")
            for index in range(count):
                key = f"agent-{index}"
                self.assertEqual(state["sticky"][f"/workspace/{index}"], key)
                self.assertEqual(state["health"][key]["last_error"],
                                 f"failure-{index}")
                self.assertEqual(state["health"][key]["consecutive"],
                                 index + 12)
                self.assertEqual(state["quota"][key]["agent"], key)

    def test_empty_and_corrupt_files_recover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = AgentSpec(key="gpt", label="Codex")
            cfg = Config(agents=[spec], data_dir=tmp)
            path = Path(tmp) / STATE_NAME
            for broken in ("", "{not-json", "[]"):
                path.write_text(broken, encoding="utf-8")
                state = load_state(cfg)
                self.assertEqual(state, {})
                state.setdefault("sticky", {})["/recovered"] = "gpt"
                self.assertTrue(save_state(cfg, state))
                self.assertEqual(
                    json.loads(path.read_text(encoding="utf-8"))["sticky"],
                    {"/recovered": "gpt"},
                )

            path.write_text(json.dumps({
                "health": {"gpt": {
                    "state": [], "until": [], "last_error": ["bad"],
                    "consecutive": {},
                }},
                "quota": [],
                "sticky": [],
            }), encoding="utf-8")
            router = Router(cfg, _NoopPool())
            restore_health(router, load_state(cfg))
            self.assertIs(router.h(spec).state, State.OK)
            self.assertEqual(router.h(spec).until, 0.0)
            self.assertEqual(router.h(spec).consecutive, 0)

    def test_persistence_errors_do_not_escape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            not_a_directory = Path(tmp) / "blocked"
            not_a_directory.write_text("file", encoding="utf-8")
            cfg = Config(agents=[], data_dir=str(not_a_directory))
            self.assertEqual(load_state(cfg), {})
            self.assertFalse(save_state(cfg, {"sticky": {"/x": "gpt"}}))
            self.assertFalse(save_state(cfg, {"bad": {1, 2, 3}}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
