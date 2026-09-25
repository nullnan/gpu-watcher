from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from gpu_watcher.models import ApiConfig, AppConfig, DaemonConfig, GpuCondition, GpuProcess, GpuState, ImNotifyConfig, Task, WorkerConfig
from gpu_watcher.scheduler import Scheduler
from gpu_watcher.store import Store


class FakeRemote:
    def __init__(
        self,
        gpu_count: int = 1,
        states: list[GpuState] | dict[str, list[GpuState]] | None = None,
        exit_codes: dict[str, int] | None = None,
        session_states: dict[str, bool] | None = None,
    ) -> None:
        self.gpu_count = gpu_count
        self.states = states
        self.exit_codes = exit_codes or {}
        self.session_states = session_states or {}
        self.started: list[tuple[str, str, list[int]]] = []
        self.killed: list[str] = []

    def probe_gpus(self, worker: WorkerConfig) -> list[GpuState]:
        if isinstance(self.states, dict):
            return self.states.get(worker.name, [])
        if self.states is not None:
            return self.states
        return [
            GpuState(index=index, memory_used_mb=0, memory_total_mb=24576, utilization_percent=0)
            for index in range(self.gpu_count)
        ]

    def start_task(self, worker: WorkerConfig, task: Task, gpu_indexes: list[int]) -> str:
        self.started.append((worker.name, task.name, gpu_indexes))
        return f"test-{task.id[:8]}"

    def read_exit_code(self, worker: WorkerConfig, task_id: str) -> int | None:
        return self.exit_codes.get(task_id)

    def session_exists(self, worker: WorkerConfig, session: str) -> bool:
        return self.session_states.get(session, True)

    def tail_logs(self, worker: WorkerConfig, task_id: str, lines: int = 20) -> str:
        return ""

    def kill_session(self, worker: WorkerConfig, session: str) -> None:
        self.killed.append(session)


class SchedulerTests(unittest.TestCase):
    def test_custom_task_condition_also_refreshes_default_gpu_condition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="custom-condition",
                command="echo custom",
                min_free_memory_mb=16000,
            )
            remote = FakeRemote(
                states=[
                    GpuState(
                        index=0,
                        memory_used_mb=12000,
                        memory_total_mb=24576,
                        utilization_percent=80,
                        numa_node=0,
                        process_users=("alice",),
                        processes=(
                            GpuProcess(
                                pid=123,
                                memory_used_mb=11900,
                                username="alice",
                                cmdline="python train.py",
                            ),
                        ),
                    )
                ]
            )

            Scheduler(config, store, remote).dispatch_queued_tasks()

            conditions = {
                state["condition_key"]: state
                for state in store.list_gpu_condition_states(worker.name)
            }
            default = GpuCondition(
                max_memory_used_mb=config.daemon.free_memory_mb,
                max_utilization_percent=config.daemon.free_utilization_percent,
            )
            custom = GpuCondition(
                max_memory_used_mb=999999,
                max_utilization_percent=config.daemon.free_utilization_percent,
                min_free_memory_mb=task.min_free_memory_mb,
            )
            self.assertEqual(set(conditions), {default.key, custom.key})
            self.assertEqual(conditions[default.key]["memory_used_mb"], 12000)
            self.assertEqual(conditions[default.key]["utilization_percent"], 80)
            self.assertEqual(conditions[default.key]["numa_node"], 0)
            self.assertEqual(conditions[default.key]["process_users"], ["alice"])
            self.assertEqual(
                conditions[default.key]["processes"],
                [
                    {
                        "pid": 123,
                        "memory_used_mb": 11900,
                        "username": "alice",
                        "cmdline": "python train.py",
                    }
                ],
            )

    def test_dispatches_highest_priority_task_to_free_gpu(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            low = store.create_task(name="low", command="echo low", priority=1)
            high = store.create_task(name="high", command="echo high", priority=10)
            remote = FakeRemote()

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "high", [0])])
            self.assertEqual(store.get_task(high.id).status, "running")
            self.assertEqual(store.get_task(low.id).status, "queued")

    def test_min_free_seconds_delays_start_until_condition_is_stable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="stable",
                command="echo stable",
                priority=1,
                min_free_seconds=60,
            )
            remote = FakeRemote()
            scheduler = Scheduler(config, store, remote)

            scheduler.dispatch_queued_tasks()

            self.assertEqual(remote.started, [])
            condition = GpuCondition(
                max_memory_used_mb=config.daemon.free_memory_mb,
                max_utilization_percent=config.daemon.free_utilization_percent,
            )
            with store.connect() as conn:
                conn.execute(
                    """
                    UPDATE gpu_condition_states
                    SET free_since = '2000-01-01T00:00:00+00:00'
                    WHERE worker_name = ? AND gpu_index = ? AND condition_key = ?
                    """,
                    (worker.name, 0, condition.key),
                )

            scheduler.dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "stable", [0])])
            self.assertEqual(store.get_task(task.id).status, "running")

    def test_finish_task_runs_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            output = Path(tmp) / "callback.txt"
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="done",
                command="echo done",
                callback_command=f"printf '%s' {{event}} > {output}",
                callback_events=["succeeded"],
            )
            store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-test")
            remote = FakeRemote(exit_codes={task.id: 0})

            Scheduler(config, store, remote).refresh_running_tasks()

            self.assertEqual(store.get_task(task.id).status, "succeeded")
            self.assertEqual(output.read_text(encoding="utf-8"), "succeeded")
            events = store.task_events(task.id)
            self.assertTrue(any(event["event"] == "callback" for event in events))

    def test_finish_task_runs_im_notify(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
                im_notify=ImNotifyConfig(enabled=True, group=123),
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(name="done", command="echo done", im_notify=True)
            store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-test")
            remote = FakeRemote(exit_codes={task.id: 0})

            with patch("gpu_watcher.scheduler.send_task_im_notification") as mocked:
                mocked.return_value = SimpleNamespace(target_type="group", target_id=123)
                Scheduler(config, store, remote).refresh_running_tasks()

            mocked.assert_called_once()
            events = store.task_events(task.id)
            self.assertTrue(any(event["event"] == "im_notify" and event["detail"] == "succeeded: ok group=123" for event in events))

    def test_missing_remote_session_fails_task_without_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(name="lost", command="echo lost")
            store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-lost")
            remote = FakeRemote(session_states={"gw-lost": False})

            Scheduler(config, store, remote).refresh_running_tasks()

            finished = store.get_task(task.id)
            self.assertEqual(finished.status, "failed")
            self.assertIsNone(finished.exit_code)
            self.assertIn("remote tmux session gw-lost disappeared", finished.message)
            self.assertTrue(any(event["event"] == "remote_session_missing" for event in store.task_events(task.id)))

    def test_missing_remote_session_requeues_preemptible_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(name="retry", command="echo retry", preemptible=True)
            store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-lost")
            remote = FakeRemote(session_states={"gw-lost": False})

            Scheduler(config, store, remote).refresh_running_tasks()

            requeued = store.get_task(task.id)
            self.assertEqual(requeued.status, "queued")
            self.assertIsNone(requeued.assigned_worker)
            self.assertTrue(any(event["event"] == "requeued" for event in store.task_events(task.id)))

    def test_unreachable_worker_preserves_tasks_until_state_can_be_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0, 1])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            retry = store.create_task(name="retry", command="echo retry", preemptible=True)
            fixed = store.create_task(name="fixed", command="echo fixed")
            store.mark_running(retry.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-retry")
            store.mark_running(fixed.id, worker_name="node-a", gpu_indexes=[1], tmux_session="gw-fixed")
            remote = FakeRemote()

            def disconnected(worker: WorkerConfig, task_id: str) -> int | None:
                raise RuntimeError("Connection closed by node-a port 22")

            remote.read_exit_code = disconnected  # type: ignore[method-assign]
            Scheduler(config, store, remote).refresh_running_tasks()

            retried = store.get_task(retry.id)
            fixed_after_probe = store.get_task(fixed.id)
            self.assertEqual(retried.status, "running")
            self.assertEqual(retried.assigned_worker, "node-a")
            self.assertEqual(fixed_after_probe.status, "running")
            self.assertEqual(fixed_after_probe.assigned_worker, "node-a")
            self.assertEqual(store.worker_healths()["node-a"]["connection_state"], "offline")
            self.assertTrue(any(event["event"] == "worker_unreachable" for event in store.task_events(retry.id)))
            self.assertFalse(any(event["event"] == "requeued" for event in store.task_events(retry.id)))
            self.assertFalse(any(event["event"] == "failed" for event in store.task_events(fixed.id)))

            remote.read_exit_code = lambda worker, task_id: 0  # type: ignore[method-assign]
            Scheduler(config, store, remote).refresh_running_tasks()

            self.assertEqual(store.get_task(retry.id).status, "succeeded")
            self.assertEqual(store.get_task(fixed.id).status, "succeeded")
            self.assertTrue(any(event["event"] == "worker_recovered" for event in store.task_events(retry.id)))

    def test_high_priority_task_preempts_lower_preemptible_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            low = store.create_task(
                name="low",
                command="echo low",
                priority=1,
                preemptible=True,
            )
            remote = FakeRemote()
            scheduler = Scheduler(config, store, remote)
            scheduler.dispatch_queued_tasks()
            high = store.create_task(
                name="high",
                command="echo high",
                priority=10,
                allow_preempt=True,
            )

            scheduler.dispatch_queued_tasks()

            self.assertEqual(store.get_task(low.id).status, "queued")
            self.assertEqual(store.get_task(high.id).status, "running")
            self.assertEqual(remote.killed, [f"test-{low.id[:8]}"])

    def test_multi_gpu_task_preempts_numa_blocker_then_restarts_it_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1, 2, 3],
                max_concurrent_tasks=4,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            blocker = store.create_task(
                name="numa-blocker",
                command="echo blocker",
                priority=1,
                preemptible=True,
                max_utilization_percent=100,
            )
            fixed = store.create_task(
                name="fixed",
                command="echo fixed",
                priority=5,
                max_utilization_percent=100,
            )
            store.mark_running(
                blocker.id,
                worker_name="node-a",
                gpu_indexes=[0],
                tmux_session="blocker-session",
            )
            store.mark_running(
                fixed.id,
                worker_name="node-a",
                gpu_indexes=[2],
                tmux_session="fixed-session",
            )
            urgent = store.create_task(
                name="urgent-local",
                command="echo urgent",
                priority=10,
                requested_gpus=2,
                allow_preempt=True,
                max_utilization_percent=100,
            )
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=10000, memory_total_mb=24576, utilization_percent=100, numa_node=0),
                    GpuState(index=1, memory_used_mb=0, memory_total_mb=24576, utilization_percent=0, numa_node=0),
                    GpuState(index=2, memory_used_mb=10000, memory_total_mb=24576, utilization_percent=100, numa_node=1),
                    GpuState(index=3, memory_used_mb=0, memory_total_mb=24576, utilization_percent=0, numa_node=1),
                ]
            )
            original_kill_session = remote.kill_session

            def release_blocker_gpu(worker: WorkerConfig, session: str) -> None:
                original_kill_session(worker, session)
                assert isinstance(remote.states, list)
                remote.states[0] = GpuState(
                    index=0,
                    memory_used_mb=0,
                    memory_total_mb=24576,
                    utilization_percent=0,
                    numa_node=0,
                )

            remote.kill_session = release_blocker_gpu  # type: ignore[method-assign]
            scheduler = Scheduler(config, store, remote)

            scheduler.dispatch_queued_tasks()

            self.assertEqual(remote.killed, ["blocker-session"])
            self.assertEqual(
                remote.started,
                [
                    ("node-a", "urgent-local", [0, 1]),
                    ("node-a", "numa-blocker", [3]),
                ],
            )
            self.assertEqual(store.get_task(urgent.id).assigned_gpus, [0, 1])
            self.assertEqual(store.get_task(blocker.id).assigned_gpus, [3])
            self.assertEqual(store.get_task(fixed.id).assigned_gpus, [2])
            events = store.task_events(urgent.id)
            self.assertTrue(any(event["event"] == "numa_preempt_requested" for event in events))

    def test_numa_locality_does_not_preempt_regular_task_without_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0, 1, 2, 3])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            blocker = store.create_task(
                name="blocker",
                command="echo blocker",
                priority=1,
                preemptible=True,
            )
            fixed = store.create_task(name="fixed", command="echo fixed", priority=5)
            store.mark_running(blocker.id, worker_name="node-a", gpu_indexes=[0], tmux_session="blocker")
            store.mark_running(fixed.id, worker_name="node-a", gpu_indexes=[2], tmux_session="fixed")
            urgent = store.create_task(
                name="no-preempt",
                command="echo urgent",
                priority=10,
                requested_gpus=2,
                max_utilization_percent=100,
            )
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=10000, memory_total_mb=24576, utilization_percent=100, numa_node=0),
                    GpuState(index=1, memory_used_mb=0, memory_total_mb=24576, utilization_percent=0, numa_node=0),
                    GpuState(index=2, memory_used_mb=10000, memory_total_mb=24576, utilization_percent=100, numa_node=1),
                    GpuState(index=3, memory_used_mb=0, memory_total_mb=24576, utilization_percent=0, numa_node=1),
                ]
            )

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.killed, [])
            self.assertEqual(remote.started, [("node-a", "no-preempt", [1, 3])])
            self.assertEqual(store.get_task(urgent.id).assigned_gpus, [1, 3])

    def test_multi_gpu_task_preempts_background_numa_blocker_without_permission_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1, 2, 3],
                max_concurrent_tasks=4,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            blocker = store.create_task(
                name="background-blocker",
                command="echo blocker",
                priority=-100,
                background=True,
                max_utilization_percent=100,
            )
            fixed = store.create_task(name="fixed", command="echo fixed", priority=5)
            store.mark_running(blocker.id, worker_name="node-a", gpu_indexes=[0], tmux_session="background")
            store.mark_running(fixed.id, worker_name="node-a", gpu_indexes=[2], tmux_session="fixed")
            urgent = store.create_task(
                name="urgent-local",
                command="echo urgent",
                priority=10,
                requested_gpus=2,
                max_utilization_percent=100,
            )
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=10000, memory_total_mb=24576, utilization_percent=100, numa_node=0),
                    GpuState(index=1, memory_used_mb=0, memory_total_mb=24576, utilization_percent=0, numa_node=0),
                    GpuState(index=2, memory_used_mb=10000, memory_total_mb=24576, utilization_percent=100, numa_node=1),
                    GpuState(index=3, memory_used_mb=0, memory_total_mb=24576, utilization_percent=0, numa_node=1),
                ]
            )
            original_kill_session = remote.kill_session

            def release_background_gpu(worker: WorkerConfig, session: str) -> None:
                original_kill_session(worker, session)
                assert isinstance(remote.states, list)
                remote.states[0] = GpuState(
                    index=0,
                    memory_used_mb=0,
                    memory_total_mb=24576,
                    utilization_percent=0,
                    numa_node=0,
                )

            remote.kill_session = release_background_gpu  # type: ignore[method-assign]

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.killed, ["background"])
            self.assertEqual(
                remote.started,
                [
                    ("node-a", "urgent-local", [0, 1]),
                    ("node-a", "background-blocker", [3]),
                ],
            )
            self.assertEqual(store.get_task(urgent.id).assigned_gpus, [0, 1])
            self.assertEqual(store.get_task(blocker.id).assigned_gpus, [3])

    def test_target_worker_restricts_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker_a = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            worker_b = WorkerConfig(name="node-b", host="node-b", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker_a, worker_b],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="targeted",
                command="echo targeted",
                priority=1,
                target_worker="node-b",
            )
            remote = FakeRemote()

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-b", "targeted", [0])])
            self.assertEqual(store.get_task(task.id).assigned_worker, "node-b")

    def test_worker_max_concurrent_tasks_limits_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1],
                max_concurrent_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            first = store.create_task(name="first", command="echo first", priority=10)
            second = store.create_task(name="second", command="echo second", priority=9)
            remote = FakeRemote(gpu_count=2)

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "first", [0])])
            self.assertEqual(store.get_task(first.id).status, "running")
            self.assertEqual(store.get_task(second.id).status, "queued")

    def test_background_tasks_run_after_regular_tasks_and_obey_background_limit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1, 2],
                max_concurrent_tasks=2,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            regular = store.create_task(name="regular", command="echo regular", priority=10)
            first_bg = store.create_task(name="bg-1", command="echo bg1", priority=1, background=True)
            second_bg = store.create_task(name="bg-2", command="echo bg2", priority=1, background=True)
            remote = FakeRemote(gpu_count=3)

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "regular", [0]), ("node-a", "bg-1", [1])])
            self.assertEqual(store.get_task(regular.id).status, "running")
            self.assertEqual(store.get_task(first_bg.id).status, "running")
            self.assertEqual(store.get_task(second_bg.id).status, "queued")

    def test_background_tasks_share_worker_total_gpu_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1],
                max_concurrent_tasks=1,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            regular = store.create_task(name="regular", command="echo regular", priority=10)
            background = store.create_task(name="bg", command="echo bg", priority=1, background=True)
            remote = FakeRemote(gpu_count=2)

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "regular", [0])])
            self.assertEqual(store.get_task(regular.id).status, "running")
            self.assertEqual(store.get_task(background.id).status, "queued")

    def test_higher_priority_regular_task_preempts_background_without_allow_preempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1],
                max_concurrent_tasks=1,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            background = store.create_task(name="bg", command="echo bg", priority=1, background=True)
            remote = FakeRemote(gpu_count=2)
            scheduler = Scheduler(config, store, remote)
            scheduler.dispatch_queued_tasks()
            urgent = store.create_task(name="urgent", command="echo urgent", priority=10)

            scheduler.dispatch_queued_tasks()

            self.assertEqual(remote.killed, [f"test-{background.id[:8]}"])
            self.assertEqual(remote.started, [("node-a", "bg", [0]), ("node-a", "urgent", [0])])
            self.assertEqual(store.get_task(background.id).status, "queued")
            self.assertEqual(store.get_task(urgent.id).status, "running")

    def test_background_preemption_runs_switch_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0],
                max_concurrent_tasks=1,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            output = Path(tmp) / "switch.txt"
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            background = store.create_task(
                name="bg",
                command="echo bg",
                priority=1,
                background=True,
                callback_command=f"printf '%s' {{event}} > {output}",
                callback_events=["switch"],
            )
            remote = FakeRemote()
            scheduler = Scheduler(config, store, remote)
            scheduler.dispatch_queued_tasks()
            store.create_task(name="urgent", command="echo urgent", priority=10)

            scheduler.dispatch_queued_tasks()

            self.assertEqual(output.read_text(encoding="utf-8"), "switch")
            events = store.task_events(background.id)
            self.assertTrue(any(event["event"] == "callback" and event["detail"] == "switch: ok" for event in events))

    def test_background_does_not_restart_while_regular_task_waits_after_preemption(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0],
                max_concurrent_tasks=1,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            background = store.create_task(
                name="bg",
                command="echo bg",
                priority=-100,
                background=True,
            )
            remote = FakeRemote()
            scheduler = Scheduler(config, store, remote)
            scheduler.dispatch_queued_tasks()
            urgent = store.create_task(
                name="urgent",
                command="echo urgent",
                priority=10,
                min_free_seconds=5,
            )

            scheduler.dispatch_queued_tasks()

            self.assertEqual(remote.killed, [f"test-{background.id[:8]}"])
            self.assertEqual(remote.started, [("node-a", "bg", [0])])
            self.assertEqual(store.get_task(background.id).status, "queued")
            self.assertEqual(store.get_task(urgent.id).status, "queued")

    def test_min_free_memory_requirement_filters_devices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0, 1])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="needs-memory",
                command="echo memory",
                priority=1,
                min_free_memory_mb=16000,
                max_utilization_percent=100,
            )
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=7000, memory_total_mb=24576, utilization_percent=100),
                    GpuState(index=1, memory_used_mb=10000, memory_total_mb=24576, utilization_percent=0),
                ],
            )

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "needs-memory", [0])])
            self.assertEqual(store.get_task(task.id).assigned_gpus, [0])

    def test_prefers_lower_utilization_gpu_when_multiple_devices_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0, 1])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="low-util",
                command="echo low-util",
                priority=1,
                min_free_memory_mb=16000,
                max_utilization_percent=100,
            )
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=7000, memory_total_mb=24576, utilization_percent=100),
                    GpuState(index=1, memory_used_mb=8000, memory_total_mb=24576, utilization_percent=0),
                ],
            )

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "low-util", [1])])
            self.assertEqual(store.get_task(task.id).assigned_gpus, [1])

    def test_multi_gpu_task_prefers_same_numa_node_before_lower_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0, 1, 2, 3])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="numa-local",
                command="echo numa",
                requested_gpus=2,
                max_utilization_percent=100,
            )
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=100, memory_total_mb=24576, utilization_percent=20, numa_node=0),
                    GpuState(index=1, memory_used_mb=100, memory_total_mb=24576, utilization_percent=20, numa_node=0),
                    GpuState(index=2, memory_used_mb=0, memory_total_mb=24576, utilization_percent=0, numa_node=1),
                    GpuState(index=3, memory_used_mb=10000, memory_total_mb=24576, utilization_percent=100, numa_node=1),
                ]
            )

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "numa-local", [0, 1])])

    def test_multi_gpu_task_falls_back_to_lower_load_without_numa_topology(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0, 1, 2])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="no-topology",
                command="echo fallback",
                requested_gpus=2,
                max_utilization_percent=100,
            )
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=100, memory_total_mb=24576, utilization_percent=30),
                    GpuState(index=1, memory_used_mb=100, memory_total_mb=24576, utilization_percent=10),
                    GpuState(index=2, memory_used_mb=100, memory_total_mb=24576, utilization_percent=20),
                ]
            )

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "no-topology", [1, 2])])

    def test_auto_worker_prefers_lower_utilization_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker_a = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            worker_b = WorkerConfig(name="node-b", host="node-b", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker_a, worker_b],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="auto-low-util",
                command="echo auto",
                priority=1,
                max_utilization_percent=100,
            )
            remote = FakeRemote(
                states={
                    "node-a": [
                        GpuState(index=0, memory_used_mb=0, memory_total_mb=24576, utilization_percent=90),
                    ],
                    "node-b": [
                        GpuState(index=0, memory_used_mb=0, memory_total_mb=24576, utilization_percent=1),
                    ],
                },
            )

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-b", "auto-low-util", [0])])
            self.assertEqual(store.get_task(task.id).assigned_worker, "node-b")

    def test_elastic_background_restarts_when_more_gpus_become_available(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1, 2],
                max_concurrent_tasks=2,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="elastic-bg",
                command="echo elastic",
                priority=-1000,
                requested_gpus=2,
                background=True,
                elastic=True,
                min_free_memory_mb=16000,
                max_utilization_percent=100,
            )
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=7000, memory_total_mb=24576, utilization_percent=0),
                    GpuState(index=1, memory_used_mb=10000, memory_total_mb=24576, utilization_percent=0),
                    GpuState(index=2, memory_used_mb=12000, memory_total_mb=24576, utilization_percent=0),
                ],
            )
            scheduler = Scheduler(config, store, remote)

            scheduler.dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "elastic-bg", [0])])

            remote.states = [
                GpuState(index=0, memory_used_mb=12000, memory_total_mb=24576, utilization_percent=100),
                GpuState(index=1, memory_used_mb=7000, memory_total_mb=24576, utilization_percent=0),
                GpuState(index=2, memory_used_mb=12000, memory_total_mb=24576, utilization_percent=0),
            ]
            scheduler.dispatch_queued_tasks()

            self.assertEqual(remote.killed, [f"test-{task.id[:8]}"])
            self.assertEqual(
                remote.started,
                [("node-a", "elastic-bg", [0]), ("node-a", "elastic-bg", [0, 1])],
            )
            self.assertEqual(store.get_task(task.id).assigned_gpus, [0, 1])

    def test_elastic_background_never_exceeds_requested_gpus(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1, 2],
                max_concurrent_tasks=3,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="elastic-one-gpu",
                command="echo elastic",
                requested_gpus=1,
                background=True,
                elastic=True,
            )
            remote = FakeRemote(gpu_count=3)

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "elastic-one-gpu", [0])])
            self.assertEqual(store.get_task(task.id).assigned_gpus, [0])

    def test_running_elastic_background_shrinks_to_requested_gpu_maximum(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1, 2],
                max_concurrent_tasks=3,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(
                name="elastic-one-gpu",
                command="echo elastic",
                requested_gpus=1,
                background=True,
                elastic=True,
            )
            store.mark_running(
                task.id,
                worker_name="node-a",
                gpu_indexes=[1, 2],
                tmux_session="elastic",
            )
            remote = FakeRemote(gpu_count=3)

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.killed, ["elastic"])
            self.assertEqual(remote.started, [("node-a", "elastic-one-gpu", [1])])
            self.assertEqual(store.get_task(task.id).assigned_gpus, [1])

    def test_elastic_background_does_not_restart_for_same_size_gpu_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1, 2],
                max_concurrent_tasks=2,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            regular = store.create_task(name="regular", command="echo regular", priority=10)
            elastic = store.create_task(
                name="elastic-bg",
                command="echo elastic",
                priority=-1000,
                background=True,
                elastic=True,
                min_free_memory_mb=16000,
                max_utilization_percent=100,
            )
            store.mark_running(regular.id, worker_name="node-a", gpu_indexes=[0], tmux_session="regular")
            store.mark_running(elastic.id, worker_name="node-a", gpu_indexes=[2], tmux_session="elastic")
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=7000, memory_total_mb=24576, utilization_percent=100),
                    GpuState(index=1, memory_used_mb=1000, memory_total_mb=24576, utilization_percent=0),
                    GpuState(index=2, memory_used_mb=9000, memory_total_mb=24576, utilization_percent=10),
                ],
            )

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.killed, [])
            self.assertEqual(remote.started, [])
            self.assertEqual(store.get_task(elastic.id).assigned_gpus, [2])

    def test_elastic_background_initial_allocation_respects_worker_gpu_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1, 2],
                max_concurrent_tasks=2,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            regular = store.create_task(name="regular", command="echo regular", priority=10)
            store.mark_running(regular.id, worker_name="node-a", gpu_indexes=[0], tmux_session="regular")
            elastic = store.create_task(
                name="elastic-bg",
                command="echo elastic",
                priority=-1000,
                background=True,
                elastic=True,
                min_free_memory_mb=16000,
                max_utilization_percent=100,
            )
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=7000, memory_total_mb=24576, utilization_percent=100),
                    GpuState(index=1, memory_used_mb=8000, memory_total_mb=24576, utilization_percent=0),
                    GpuState(index=2, memory_used_mb=9000, memory_total_mb=24576, utilization_percent=0),
                ],
            )

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.started, [("node-a", "elastic-bg", [1])])
            self.assertEqual(store.get_task(elastic.id).assigned_gpus, [1])

    def test_elastic_background_shrinks_when_other_tasks_use_worker_gpu_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1, 2],
                max_concurrent_tasks=2,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            regular = store.create_task(name="regular", command="echo regular", priority=10)
            elastic = store.create_task(
                name="elastic-bg",
                command="echo elastic",
                priority=-1000,
                background=True,
                elastic=True,
                min_free_memory_mb=16000,
                max_utilization_percent=100,
            )
            store.mark_running(regular.id, worker_name="node-a", gpu_indexes=[0], tmux_session="regular")
            store.mark_running(elastic.id, worker_name="node-a", gpu_indexes=[1, 2], tmux_session="elastic")
            remote = FakeRemote(
                states=[
                    GpuState(index=0, memory_used_mb=7000, memory_total_mb=24576, utilization_percent=100),
                    GpuState(index=1, memory_used_mb=8000, memory_total_mb=24576, utilization_percent=0),
                    GpuState(index=2, memory_used_mb=9000, memory_total_mb=24576, utilization_percent=0),
                ],
            )

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.killed, ["elastic"])
            self.assertEqual(remote.started, [("node-a", "elastic-bg", [1])])
            self.assertEqual(store.get_task(elastic.id).assigned_gpus, [1])

    def test_elastic_background_is_suspended_when_worker_gpu_cap_is_exhausted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(
                name="node-a",
                host="node-a",
                gpus=[0, 1, 2],
                max_concurrent_tasks=2,
                max_background_tasks=1,
            )
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            regular = store.create_task(name="regular", command="echo regular", priority=10, requested_gpus=2)
            elastic = store.create_task(
                name="elastic-bg",
                command="echo elastic",
                priority=-1000,
                background=True,
                elastic=True,
                min_free_memory_mb=16000,
                max_utilization_percent=100,
            )
            store.mark_running(regular.id, worker_name="node-a", gpu_indexes=[0, 1], tmux_session="regular")
            store.mark_running(elastic.id, worker_name="node-a", gpu_indexes=[2], tmux_session="elastic")
            remote = FakeRemote(gpu_count=3)

            Scheduler(config, store, remote).dispatch_queued_tasks()

            self.assertEqual(remote.killed, ["elastic"])
            self.assertEqual(store.get_task(elastic.id).status, "queued")
            self.assertEqual(store.get_task(elastic.id).assigned_gpus, [])
            events = store.task_events(elastic.id)
            self.assertTrue(any(event["event"] == "elastic_suspended" for event in events))

    def test_manual_start_dispatches_paused_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(name="manual", command="echo manual", paused=True)
            remote = FakeRemote()

            changed = Scheduler(config, store, remote).start_task(task.id)

            self.assertTrue(changed)
            self.assertEqual(remote.started, [("node-a", "manual", [0])])
            self.assertEqual(store.get_task(task.id).status, "running")

    def test_pause_running_task_kills_session_and_holds_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(name="manual", command="echo manual", preemptible=True)
            remote = FakeRemote()
            scheduler = Scheduler(config, store, remote)
            scheduler.dispatch_queued_tasks()

            changed = scheduler.pause_task(task.id)

            self.assertTrue(changed)
            self.assertEqual(remote.killed, [f"test-{task.id[:8]}"])
            paused = store.get_task(task.id)
            self.assertEqual(paused.status, "paused")
            self.assertEqual(paused.assigned_gpus, [])
            self.assertIsNone(paused.assigned_worker)

    def test_pause_rejects_non_preemptible_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(name="manual", command="echo manual")
            remote = FakeRemote()
            scheduler = Scheduler(config, store, remote)
            scheduler.dispatch_queued_tasks()

            changed = scheduler.pause_task(task.id)

            self.assertFalse(changed)
            self.assertEqual(remote.killed, [])
            self.assertEqual(store.get_task(task.id).status, "running")

    def test_terminate_running_task_kills_session_and_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(name="manual", command="echo manual")
            remote = FakeRemote()
            scheduler = Scheduler(config, store, remote)
            scheduler.dispatch_queued_tasks()

            changed = scheduler.cancel_task(task.id)

            self.assertTrue(changed)
            self.assertEqual(remote.killed, [f"test-{task.id[:8]}"])
            self.assertEqual(store.get_task(task.id).status, "canceled")

    def test_rerun_failed_task_requeues_and_dispatches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
            config = AppConfig(
                daemon=DaemonConfig(db_path=str(Path(tmp) / "test.db")),
                api=ApiConfig(enabled=False),
                workers=[worker],
            )
            store = Store(config.daemon.db_path)
            store.init()
            store.sync_workers(config.workers)
            task = store.create_task(name="retry", command="echo retry")
            store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="old")
            store.finish_task(task.id, exit_code=2)
            remote = FakeRemote()

            changed = Scheduler(config, store, remote).rerun_task(task.id)

            self.assertTrue(changed)
            self.assertEqual(remote.started, [("node-a", "retry", [0])])
            rerun = store.get_task(task.id)
            self.assertEqual(rerun.status, "running")
            self.assertIsNone(rerun.exit_code)


if __name__ == "__main__":
    unittest.main()
