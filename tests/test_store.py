from __future__ import annotations

import tempfile
import unittest
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from gpu_watcher.store import Store


class StoreTests(unittest.TestCase):
    def test_wal_files_remain_between_operations_and_close_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.db"
            store = Store(path)
            self.addCleanup(store.close)
            store.init()
            shm = Path(f"{path}-shm")
            inode = shm.stat().st_ino
            for index in range(20):
                store.create_task(name=str(index), command="true")
                store.list_tasks()
                self.assertEqual(shm.stat().st_ino, inode)
            store.close()
            self.assertFalse(shm.exists())
            self.assertFalse(Path(f"{path}-wal").exists())
            with self.assertRaisesRegex(RuntimeError, "Store is closed"):
                store.list_tasks()

    def test_failed_transaction_is_rolled_back_before_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            self.addCleanup(store.close)
            store.init()
            task = store.create_task(name="original", command="true")
            with self.assertRaisesRegex(ValueError, "abort"):
                with store.connect() as conn:
                    conn.execute("UPDATE tasks SET name = 'uncommitted'")
                    raise ValueError("abort")
            self.assertEqual(store.get_task(task.id).name, "original")
            store.create_task(name="committed", command="true")
            self.assertEqual(store.count_tasks(), 2)

    def test_concurrent_threads_borrow_independent_connections(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            self.addCleanup(store.close)
            store.init()
            barrier = threading.Barrier(4)

            def write(index: int) -> None:
                with store.connect() as conn:
                    barrier.wait(timeout=10)
                    self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                for number in range(10):
                    store.create_task(name=f"{index}-{number}", command="true")

            with ThreadPoolExecutor(max_workers=4) as executor:
                list(executor.map(write, range(4)))
            self.assertEqual(store.count_tasks(), 40)

    def test_tasks_can_be_searched_by_name_or_id_substring(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()
            matching_name = store.create_task(name="ResNet Training", command="true")
            matching_id = store.create_task(name="other", command="true")
            store.create_task(name="unrelated", command="true")

            by_name = store.list_tasks(search="net train")
            by_id = store.list_tasks(search=matching_id.id[4:12])

            self.assertEqual([task.id for task in by_name], [matching_name.id])
            self.assertEqual([task.id for task in by_id], [matching_id.id])
            self.assertEqual(store.count_tasks(search="TRAIN"), 1)

    def test_queued_tasks_are_ordered_by_priority_then_time(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()
            low = store.create_task(name="low", command="echo low", priority=1)
            high = store.create_task(name="high", command="echo high", priority=10)

            queued = store.queued_tasks()

            self.assertEqual([task.id for task in queued], [high.id, low.id])
            self.assertEqual(high.log_mode, "pty")

    def test_task_can_use_pipe_log_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()

            task = store.create_task(name="pipe", command="echo pipe", log_mode="pipe")

            self.assertEqual(task.log_mode, "pipe")
            self.assertEqual(task.as_dict()["log_mode"], "pipe")

    def test_task_can_store_callback_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()

            task = store.create_task(
                name="notify",
                command="echo notify",
                callback_command="notify {title} {content}",
                callback_events=["complete", "failure", "switch"],
            )

            self.assertEqual(task.callback_command, "notify {title} {content}")
            self.assertEqual(task.callback_events, ["succeeded", "failed", "switch"])
            self.assertEqual(task.as_dict()["callback_events"], ["succeeded", "failed", "switch"])

    def test_task_can_store_im_notify_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()

            task = store.create_task(name="notify", command="echo notify", im_notify=True)

            self.assertTrue(task.im_notify)
            self.assertTrue(task.as_dict()["im_notify"])

    def test_paused_task_options_can_all_be_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()
            task = store.create_task(name="old", command="echo old", paused=True)

            updated = store.update_task(
                task.id,
                name="new",
                command="python train.py --new",
                priority=42,
                requested_gpus=2,
                env={"MODE": "train"},
                cwd="/srv/train",
                target_worker="node-a",
                max_memory_used_mb=1024,
                min_free_memory_mb=16000,
                max_utilization_percent=25,
                min_free_seconds=30,
                preemptible=True,
                allow_preempt=True,
                background=True,
                elastic=True,
                log_mode="pipe",
                im_notify=True,
                callback_command="notify {title}",
                callback_events=["complete", "failure"],
            )

            self.assertIsNotNone(updated)
            self.assertEqual(updated.status, "paused")
            self.assertEqual(updated.name, "new")
            self.assertEqual(updated.command, "python train.py --new")
            self.assertEqual(updated.priority, 42)
            self.assertEqual(updated.requested_gpus, 2)
            self.assertEqual(updated.env, {"MODE": "train"})
            self.assertEqual(updated.cwd, "/srv/train")
            self.assertEqual(updated.target_worker, "node-a")
            self.assertEqual(updated.max_memory_used_mb, 1024)
            self.assertEqual(updated.min_free_memory_mb, 16000)
            self.assertEqual(updated.max_utilization_percent, 25)
            self.assertEqual(updated.min_free_seconds, 30)
            self.assertTrue(updated.preemptible)
            self.assertTrue(updated.allow_preempt)
            self.assertTrue(updated.background)
            self.assertTrue(updated.elastic)
            self.assertEqual(updated.log_mode, "pipe")
            self.assertTrue(updated.im_notify)
            self.assertEqual(updated.callback_command, "notify {title}")
            self.assertEqual(updated.callback_events, ["succeeded", "failed"])
            self.assertTrue(any(event["event"] == "updated" for event in store.task_events(task.id)))

    def test_running_task_options_cannot_be_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()
            task = store.create_task(name="running", command="echo old")
            store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-running")

            updated = store.update_task(
                task.id,
                name="changed",
                command="echo changed",
                priority=0,
                requested_gpus=1,
                env={},
                cwd=None,
                target_worker=None,
                max_memory_used_mb=None,
                min_free_memory_mb=None,
                max_utilization_percent=None,
                min_free_seconds=0,
                preemptible=False,
                allow_preempt=False,
                background=False,
                elastic=False,
                log_mode="pty",
                im_notify=False,
                callback_command=None,
                callback_events=None,
            )

            self.assertIsNone(updated)
            self.assertEqual(store.get_task(task.id).command, "echo old")

    def test_paused_task_is_not_queued_until_started(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()
            task = store.create_task(name="hold", command="echo hold", paused=True)

            self.assertEqual(task.status, "paused")
            self.assertEqual(store.queued_tasks(), [])
            self.assertTrue(store.start_task(task.id))
            self.assertEqual(store.get_task(task.id).status, "queued")

    def test_paused_task_can_be_terminated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()
            task = store.create_task(name="hold", command="echo hold", paused=True)

            self.assertTrue(store.cancel_task(task.id))

            self.assertEqual(store.get_task(task.id).status, "canceled")

    def test_pause_requires_preemptible_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()
            task = store.create_task(name="fixed", command="echo fixed")

            self.assertFalse(store.pause_task(task.id))

            self.assertEqual(store.get_task(task.id).status, "queued")

    def test_failed_task_can_be_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()
            task = store.create_task(name="retry", command="echo retry")
            store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-test")
            store.finish_task(task.id, exit_code=1)

            self.assertTrue(store.rerun_task(task.id))

            rerun = store.get_task(task.id)
            self.assertEqual(rerun.status, "queued")
            self.assertIsNone(rerun.exit_code)
            self.assertIsNone(rerun.finished_at)
            self.assertEqual(rerun.assigned_gpus, [])

    def test_finished_task_dict_includes_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / "test.db")
            store.init()
            task = store.create_task(name="timed", command="echo timed")
            store.mark_running(
                task.id,
                worker_name="node-a",
                gpu_indexes=[0],
                tmux_session="gw-test",
            )
            store.finish_task(task.id, exit_code=0)

            finished = store.get_task(task.id)

            self.assertIsNotNone(finished.as_dict()["duration_seconds"])
            self.assertGreaterEqual(finished.as_dict()["duration_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
