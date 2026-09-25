from __future__ import annotations

from datetime import UTC, datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from gpu_watcher.cron import CronExpression
from gpu_watcher.models import ApiConfig, AppConfig, DaemonConfig, TaskStatus, WorkerConfig
from gpu_watcher.scheduler import Scheduler
from gpu_watcher.store import Store


class CronExpressionTests(unittest.TestCase):
    def test_matches_lists_ranges_steps_names_and_cron_day_rule(self) -> None:
        expression = CronExpression.parse("*/15 9-17/2 1,15 jan,mar mon-fri")
        self.assertTrue(expression.matches(datetime(2026, 1, 5, 9, 30, tzinfo=UTC)))
        self.assertFalse(expression.matches(datetime(2026, 1, 5, 10, 30, tzinfo=UTC)))
        self.assertFalse(expression.matches(datetime(2026, 2, 5, 9, 30, tzinfo=UTC)))

        # 0 and 7 both mean Sunday; a 0-7 range covers all weekdays.
        every_weekday = CronExpression.parse("0 9 * * 0-7")
        self.assertTrue(every_weekday.matches(datetime(2026, 1, 6, 9, 0, tzinfo=UTC)))

        # When both day fields are restricted, classic cron treats them as OR.
        day_rule = CronExpression.parse("0 9 1 * mon")
        self.assertTrue(day_rule.matches(datetime(2026, 2, 1, 9, 0, tzinfo=UTC)))
        self.assertTrue(day_rule.matches(datetime(2026, 2, 2, 9, 0, tzinfo=UTC)))

    def test_rejects_invalid_expression(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly 5 fields"):
            CronExpression.parse("0 9 * *")
        with self.assertRaisesRegex(ValueError, "minute"):
            CronExpression.parse("60 9 * * *")


class TaskScheduleTests(unittest.TestCase):
    def _scheduler(self, path: Path) -> tuple[Store, Scheduler]:
        worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
        config = AppConfig(
            daemon=DaemonConfig(db_path=str(path)),
            api=ApiConfig(enabled=False),
            workers=[worker],
        )
        store = Store(path)
        store.init()
        store.sync_workers(config.workers)
        return store, Scheduler(config, store, SimpleNamespace(kill_session=lambda worker, session: None))

    def test_schedule_is_persisted_and_invalid_values_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, _ = self._scheduler(Path(tmp) / "tasks.db")
            task = store.create_task(
                name="work-hours",
                command="sleep 3600",
                paused=True,
                start_cron="0 9 * * mon-fri",
                stop_cron="0 18 * * mon-fri",
                schedule_timezone="Asia/Shanghai",
            )
            saved = store.get_task(task.id)
            self.assertEqual(saved.start_cron, "0 9 * * mon-fri")
            self.assertEqual(saved.stop_cron, "0 18 * * mon-fri")
            self.assertEqual(saved.schedule_timezone, "Asia/Shanghai")
            with self.assertRaisesRegex(ValueError, "cron"):
                store.create_task(name="bad", command="true", start_cron="every day")
            with self.assertRaisesRegex(ValueError, "timezone"):
                store.create_task(name="bad-zone", command="true", schedule_timezone="Mars/Olympus")

    def test_start_schedule_runs_once_per_minute_and_stop_pauses_nonpreemptible_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, scheduler = self._scheduler(Path(tmp) / "tasks.db")
            task = store.create_task(
                name="scheduled",
                command="sleep 3600",
                paused=True,
                start_cron="0 9 * * *",
                stop_cron="0 18 * * *",
                schedule_timezone="Asia/Shanghai",
            )

            scheduler._apply_schedules(datetime(2026, 9, 4, 1, 0, tzinfo=UTC))
            self.assertEqual(store.get_task(task.id).status, TaskStatus.QUEUED.value)
            scheduler._apply_schedules(datetime(2026, 9, 4, 1, 0, 45, tzinfo=UTC))
            starts = [event for event in store.task_events(task.id) if event["event"] == "scheduled_start"]
            self.assertEqual(len(starts), 1)

            store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-scheduled")
            scheduler._apply_schedules(datetime(2026, 9, 4, 10, 0, tzinfo=UTC))
            self.assertEqual(store.get_task(task.id).status, TaskStatus.PAUSED.value)
            events = store.task_events(task.id)
            self.assertTrue(any(event["event"] == "scheduled_stop" for event in events))
