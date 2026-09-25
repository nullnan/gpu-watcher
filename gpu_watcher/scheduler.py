from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo
from itertools import combinations
from subprocess import TimeoutExpired
from typing import Any

from .callbacks import run_task_callback
from .cron import CronExpression
from .config import ConfigError, update_worker_config_file
from .im_notify import send_task_im_notification
from .models import AppConfig, GpuCondition, GpuState, Task, TaskStatus, WorkerConfig
from .remote import RemoteExecutor
from .store import Store

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class DispatchPlan:
    worker: WorkerConfig
    gpu_indexes: list[int]


@dataclass(frozen=True)
class NumaPreemptionPlan:
    worker: WorkerConfig
    numa_node: int
    victims: list[Task]


class Scheduler:
    def __init__(self, config: AppConfig, store: Store, remote: RemoteExecutor | None = None):
        self.config = config
        self.store = store
        self.remote = remote or RemoteExecutor(config.daemon.remote_base_dir)
        self.workers = {worker.name: worker for worker in config.workers}
        self._dispatch_lock = threading.RLock()

    def run_forever(self) -> None:
        LOGGER.info("scheduler started with %d workers", len(self.config.workers))
        while True:
            try:
                self.tick()
            except Exception:
                LOGGER.exception("scheduler tick failed")
            time.sleep(self.config.daemon.poll_interval_seconds)

    def tick(self, now: datetime | None = None) -> None:
        """Refresh task state and apply any cron controls due in this minute."""
        with self._dispatch_lock:
            self._apply_schedules(now or datetime.now(UTC))
            self.refresh_running_tasks()
            self._dispatch_queued_tasks()

    def _apply_schedules(self, now: datetime) -> None:
        """Apply start/stop schedules once per UTC minute for each task.

        Stop wins when both expressions match the same minute, so a task cannot
        be launched only to be immediately stopped by a conflicting schedule.
        """
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        current = now.astimezone(UTC).replace(second=0, microsecond=0)
        minute_key = current.isoformat()
        for task in self.store.scheduled_tasks():
            local_now = current.astimezone(ZoneInfo(task.schedule_timezone))
            start_due = bool(task.start_cron and CronExpression.parse(task.start_cron).matches(local_now))
            stop_due = bool(task.stop_cron and CronExpression.parse(task.stop_cron).matches(local_now))
            if stop_due:
                if self.store.claim_schedule_trigger(task.id, "stop", minute_key):
                    self._scheduled_stop(task, f"scheduled stop ({task.stop_cron})")
                continue
            if start_due and self.store.claim_schedule_trigger(task.id, "start", minute_key):
                self._scheduled_start(task, f"scheduled start ({task.start_cron})")

    def _scheduled_start(self, task: Task, message: str) -> bool:
        current = self.store.get_task(task.id)
        if current is None:
            return False
        if current.status == TaskStatus.PAUSED.value:
            return self.store.start_task(task.id, message, event="scheduled_start")
        if current.status == TaskStatus.QUEUED.value:
            self.store.record_event(task.id, "scheduled_start", message)
            return True
        return False

    def _scheduled_stop(self, task: Task, message: str) -> bool:
        current = self.store.get_task(task.id)
        if current is None or current.status not in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value}:
            return False
        if current.status == TaskStatus.RUNNING.value and current.assigned_worker:
            worker = self.workers.get(current.assigned_worker)
            if worker:
                try:
                    self._kill_task(worker, current)
                except Exception as exc:
                    self.store.record_event(current.id, "scheduled_stop_kill_failed", str(exc))
                    return False
        # A schedule is explicit task ownership, so it may pause a task even
        # when manual preemption is disabled.
        return self.store.pause_task(
            current.id,
            message,
            require_preemptible=False,
            event="scheduled_stop",
        )

    def refresh_running_tasks(self) -> None:
        unreachable_workers: set[str] = set()
        for task in self.store.running_tasks():
            if not task.assigned_worker:
                continue
            worker = self.workers.get(task.assigned_worker)
            if not worker:
                self.store.record_event(task.id, "worker_missing", task.assigned_worker)
                continue
            if worker.name in unreachable_workers:
                continue
            try:
                exit_code = self.remote.read_exit_code(worker, task.id)
            except Exception as exc:
                unreachable_workers.add(worker.name)
                self._mark_worker_unreachable(worker, str(exc))
                continue
            self._mark_worker_reachable(worker)
            if exit_code is None:
                try:
                    session_exists = self._session_exists(worker, task)
                except Exception as exc:
                    unreachable_workers.add(worker.name)
                    self._mark_worker_unreachable(worker, str(exc))
                    continue
                if session_exists is None or session_exists:
                    continue
                session = task.tmux_session or self.remote.session_name(worker, task.id)
                message = (
                    f"remote tmux session {session} disappeared before exit_code was written; "
                    "the task was likely terminated outside gpu-watcher"
                )
                self.store.record_event(task.id, "remote_session_missing", message)
                try:
                    tail = self.remote.tail_logs(worker, task.id)
                except Exception as exc:
                    self.store.record_event(task.id, "log_check_failed", str(exc))
                else:
                    if tail:
                        message = f"{message}; last log:\n{tail[-4000:]}"
                self._requeue_or_fail_lost_task(task, message)
                LOGGER.warning("task %s lost remote tmux session before exit code", task.id)
                continue
            message = self.remote.tail_logs(worker, task.id)
            self.store.finish_task(task.id, exit_code=exit_code, message=message[-4000:] if message else None)
            finished = self.store.get_task(task.id) or task
            callback_event = TaskStatus.SUCCEEDED.value if exit_code == 0 else TaskStatus.FAILED.value
            self._run_task_notifications(finished, callback_event, message)
            LOGGER.info("task %s finished with exit code %s", task.id, exit_code)

    def _session_exists(self, worker: WorkerConfig, task: Task) -> bool | None:
        """Probe a running task's tmux session, preserving compatibility with test remotes."""
        probe = getattr(self.remote, "session_exists", None)
        if not callable(probe):
            return None
        session = task.tmux_session or self.remote.session_name(worker, task.id)
        return bool(probe(worker, session))

    def _mark_worker_unreachable(self, worker: WorkerConfig, reason: str) -> None:
        """Persist an outage without guessing the state of remote tasks.

        A network failure is not evidence that a task stopped. Keeping tasks in
        RUNNING lets a later successful probe reconcile their exit code or tmux
        session without launching a duplicate copy elsewhere.
        """
        detail = reason.strip() or "worker probe failed"
        previous = self.store.record_worker_probe(worker.name, reachable=False, error=detail)
        LOGGER.warning("worker %s is unreachable: %s", worker.name, detail)
        if previous == "offline":
            return
        for task in self.store.running_tasks_for_worker(worker.name):
            message = f"worker {worker.name} is unreachable: {detail}"
            self.store.record_event(task.id, "worker_unreachable", message)

    def _mark_worker_reachable(self, worker: WorkerConfig) -> None:
        previous = self.store.record_worker_probe(worker.name, reachable=True)
        if previous != "offline":
            return
        LOGGER.info("worker %s is reachable again", worker.name)
        for task in self.store.running_tasks_for_worker(worker.name):
            self.store.record_event(
                task.id,
                "worker_recovered",
                f"worker {worker.name} is reachable again; reconciling remote task state",
            )

    def _requeue_or_fail_lost_task(self, task: Task, message: str) -> None:
        """Retry preemptible work after a lost worker; fail non-preemptible work."""
        if task.preemptible:
            self.store.requeue_task(task.id, message)
            requeued = self.store.get_task(task.id) or task
            self._run_task_notifications(requeued, "switch", message)
            LOGGER.warning("preemptible task %s requeued: %s", task.id, message)
            return
        self.store.fail_task(task.id, message=message)
        failed = self.store.get_task(task.id) or task
        self._run_task_notifications(failed, TaskStatus.FAILED.value, message)

    def dispatch_queued_tasks(self) -> None:
        with self._dispatch_lock:
            self._dispatch_queued_tasks()

    def _dispatch_queued_tasks(self) -> None:
        queued = self.store.queued_tasks()
        running_elastic = [
            task
            for task in self.store.running_tasks()
            if task.background and task.elastic and task.assigned_worker
        ]
        condition_tasks = queued + running_elastic
        conditions = self._conditions_for_tasks(condition_tasks) if condition_tasks else [self._default_condition()]
        worker_states = self._probe_workers(conditions)
        if not queued:
            self._resize_elastic_background_tasks(worker_states)
            return

        attempted_regular: set[str] = set()
        while True:
            task = next(
                (
                    queued_task
                    for queued_task in self.store.queued_tasks()
                    if not queued_task.background and queued_task.id not in attempted_regular
                ),
                None,
            )
            if task is None:
                break
            attempted_regular.add(task.id)
            plan = self._find_plan(task, worker_states)

            if plan is not None and not self._plan_is_numa_local(task, plan, worker_states):
                numa_preemption = self._find_numa_preemption_plan(task, worker_states)
                if numa_preemption is not None and self._apply_numa_preemption(task, numa_preemption):
                    self._refresh_worker_state(numa_preemption.worker, worker_states, conditions)
                    local_plan = self._find_plan(task, worker_states)
                    if local_plan is None or not self._plan_is_numa_local(task, local_plan, worker_states):
                        continue
                    plan = local_plan

            if plan is None:
                preempted_worker = self._preempt_background_for_task(task, worker_states)
                if preempted_worker is not None:
                    self._refresh_worker_state(preempted_worker, worker_states, conditions)
                    plan = self._find_plan(task, worker_states)
                    if plan is not None:
                        self._start_with_requeue_on_failure(task, plan)
                    continue
                if task.allow_preempt:
                    preempted_worker = self._preempt_for_task(task, worker_states)
                    if preempted_worker is not None:
                        self._refresh_worker_state(preempted_worker, worker_states, conditions)
                        plan = self._find_plan(task, worker_states)
                        if plan is not None:
                            self._start_with_requeue_on_failure(task, plan)
                continue
            session = self._start_with_requeue_on_failure(task, plan)
            if not session:
                continue

        self._resize_elastic_background_tasks(worker_states)
        background_tasks = [
            task
            for task in self.store.queued_tasks()
            if task.background
        ]
        for task in background_tasks:
            plan = self._find_plan(task, worker_states)
            if plan is None:
                continue
            if self._worker_has_pending_regular_task(plan.worker, task):
                continue
            self._start_with_requeue_on_failure(task, plan)

    def start_task(self, task_id: str, message: str = "manual start requested") -> bool:
        task = self.store.get_task(task_id)
        if task is None:
            return False
        if task.status == TaskStatus.PAUSED.value:
            changed = self.store.start_task(task_id, message)
        elif task.status == TaskStatus.QUEUED.value:
            self.store.record_event(task_id, "start_requested", message)
            changed = True
        else:
            return False
        if changed:
            self.dispatch_queued_tasks()
        return changed

    def update_task(self, task_id: str, **options: Any) -> Task | None:
        with self._dispatch_lock:
            return self.store.update_task(task_id, **options)

    def update_worker(self, current_name: str, worker: WorkerConfig) -> WorkerConfig:
        with self._dispatch_lock:
            if current_name not in self.workers:
                raise ConfigError(f"worker not found: {current_name}")
            if worker.name != current_name:
                raise ConfigError("worker name cannot be changed")
            if not self.config.config_path:
                raise ConfigError("daemon config path is unavailable")

            update_worker_config_file(self.config.config_path, current_name, worker)
            for index, configured in enumerate(self.config.workers):
                if configured.name == current_name:
                    self.config.workers[index] = worker
                    break
            self.workers[current_name] = worker
            self.store.sync_workers([worker])
            return worker

    def pause_task(self, task_id: str, message: str = "paused by user") -> bool:
        task = self.store.get_task(task_id)
        if task is None:
            return False
        if not task.preemptible:
            self.store.record_event(task.id, "pause_rejected", "task is not preemptible")
            return False
        if task.status not in {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value}:
            return False
        if task.status == TaskStatus.RUNNING.value and task.assigned_worker:
            worker = self.workers.get(task.assigned_worker)
            if worker:
                try:
                    self._kill_task(worker, task)
                except Exception as exc:
                    self.store.record_event(task.id, "pause_kill_failed", str(exc))
                    return False
        return self.store.pause_task(task_id, message)

    def rerun_task(self, task_id: str, message: str = "rerun requested") -> bool:
        task = self.store.get_task(task_id)
        if task is None:
            return False
        if task.status != TaskStatus.FAILED.value:
            return False
        changed = self.store.rerun_task(task_id, message)
        if changed:
            self.dispatch_queued_tasks()
        return changed

    def cancel_task(self, task_id: str, message: str = "terminated by user") -> bool:
        task = self.store.get_task(task_id)
        if task is None:
            return False
        if task.status == TaskStatus.RUNNING.value and task.assigned_worker:
            worker = self.workers.get(task.assigned_worker)
            if worker:
                try:
                    self._kill_task(worker, task)
                except Exception as exc:
                    self.store.record_event(task.id, "cancel_kill_failed", str(exc))
        return self.store.cancel_task(task_id, message)

    def _kill_task(self, worker: WorkerConfig, task: Task) -> None:
        if hasattr(self.remote, "kill_task"):
            self.remote.kill_task(worker, task)
        elif task.tmux_session:
            self.remote.kill_session(worker, task.tmux_session)

    def _probe_workers(self, conditions: list[GpuCondition]) -> dict[str, list[GpuState]]:
        worker_states: dict[str, list[GpuState]] = {}
        for worker in self.config.workers:
            if not worker.enabled:
                continue
            try:
                states = self.remote.probe_gpus(worker)
            except Exception as exc:
                self._mark_worker_unreachable(worker, str(exc))
                LOGGER.warning("worker %s GPU probe failed: %s", worker.name, exc)
                continue
            self._mark_worker_reachable(worker)
            self.store.update_gpu_condition_states(worker.name, states, conditions)
            worker_states[worker.name] = states
        return worker_states

    def _refresh_worker_state(
        self,
        worker: WorkerConfig,
        worker_states: dict[str, list[GpuState]],
        conditions: list[GpuCondition],
    ) -> bool:
        try:
            states = self.remote.probe_gpus(worker)
        except Exception as exc:
            self._mark_worker_unreachable(worker, str(exc))
            LOGGER.warning("worker %s GPU probe after preemption failed: %s", worker.name, exc)
            worker_states.pop(worker.name, None)
            return False
        self._mark_worker_reachable(worker)
        self.store.update_gpu_condition_states(worker.name, states, conditions)
        worker_states[worker.name] = states
        return True

    def _available_gpus_by_worker(
        self,
        queued_task: Task,
        worker_states: dict[str, list[GpuState]],
        *,
        exclude_task_id: str | None = None,
    ) -> dict[str, list[GpuState]]:
        running = self.store.running_tasks()
        occupied: dict[str, set[int]] = {}
        for running_task in running:
            if exclude_task_id and running_task.id == exclude_task_id:
                continue
            if running_task.assigned_worker:
                occupied.setdefault(running_task.assigned_worker, set()).update(running_task.assigned_gpus)

        available: dict[str, list[GpuState]] = {}
        for worker in self.config.workers:
            if not worker.enabled:
                continue
            states = worker_states.get(worker.name, [])
            busy = occupied.get(worker.name, set())
            free_states = [
                gpu
                for gpu in states
                if gpu.index not in busy
                and self._gpu_satisfies_task(worker.name, gpu, queued_task)
            ]
            available[worker.name] = self._sort_gpus_by_preference(free_states)
        return available

    def _find_plan(self, task: Task, worker_states: dict[str, list[GpuState]]) -> DispatchPlan | None:
        available = self._available_gpus_by_worker(task, worker_states)
        running_gpu_slots = self._running_gpu_slots_by_worker()
        running_background_counts = self._running_background_counts_by_worker()
        worker_order = {worker.name: index for index, worker in enumerate(self.config.workers)}
        candidates: list[tuple[tuple[int, float, int, int, int, int], DispatchPlan]] = []
        for worker in self.config.workers:
            if not worker.enabled:
                continue
            if task.target_worker and worker.name != task.target_worker:
                continue
            if task.background and running_background_counts.get(worker.name, 0) >= worker.max_background_tasks:
                continue
            free = available.get(worker.name, [])
            remaining_slots = len(free)
            if worker.max_concurrent_tasks is not None:
                remaining_slots = max(
                    0,
                    worker.max_concurrent_tasks - running_gpu_slots.get(worker.name, 0),
                )
            target_count = self._target_gpu_count(task, worker, free)
            target_count = min(target_count, remaining_slots)
            minimum_count = 1 if task.elastic else task.requested_gpus
            if target_count >= minimum_count:
                selected = self._select_gpu_set(free, target_count)
                candidates.append((
                    self._plan_preference_key(selected, worker_order.get(worker.name, 0)),
                    DispatchPlan(
                        worker=worker,
                        gpu_indexes=[gpu.index for gpu in selected],
                    ),
                ))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def _plan_is_numa_local(
        self,
        task: Task,
        plan: DispatchPlan,
        worker_states: dict[str, list[GpuState]],
    ) -> bool:
        if task.requested_gpus <= 1:
            return True
        state_by_index = {
            gpu.index: gpu
            for gpu in worker_states.get(plan.worker.name, [])
        }
        selected = [state_by_index.get(index) for index in plan.gpu_indexes]
        if any(gpu is None or gpu.numa_node is None for gpu in selected):
            return False
        return len({gpu.numa_node for gpu in selected if gpu is not None}) == 1

    def _find_numa_preemption_plan(
        self,
        task: Task,
        worker_states: dict[str, list[GpuState]],
    ) -> NumaPreemptionPlan | None:
        if task.requested_gpus <= 1 or task.elastic or task.min_free_seconds > 0:
            return None

        available_by_worker = self._available_gpus_by_worker(task, worker_states)
        worker_order = {worker.name: index for index, worker in enumerate(self.config.workers)}
        candidates: list[tuple[tuple[int, int, int, int, str, int, int], NumaPreemptionPlan]] = []
        for worker in self.config.workers:
            if not worker.enabled or worker.name not in worker_states:
                continue
            if task.target_worker and worker.name != task.target_worker:
                continue

            states = worker_states[worker.name]
            state_by_index = {gpu.index: gpu for gpu in states}
            free_indexes = {gpu.index for gpu in available_by_worker.get(worker.name, [])}
            running_tasks = self.store.running_tasks_for_worker(worker.name)
            used_gpu_slots = sum(len(running.assigned_gpus) for running in running_tasks)
            eligible = [
                running
                for running in running_tasks
                if running.priority < task.priority
                and (
                    running.background
                    or (task.allow_preempt and running.preemptible)
                )
            ]
            eligible.sort(
                key=lambda running: (
                    not running.background,
                    running.priority,
                    running.started_at or "",
                )
            )

            numa_nodes = sorted(
                {
                    gpu.numa_node
                    for gpu in states
                    if gpu.numa_node is not None
                }
            )
            for numa_node in numa_nodes:
                free_on_numa = {
                    index
                    for index in free_indexes
                    if state_by_index[index].numa_node == numa_node
                }
                if len(free_on_numa) >= task.requested_gpus:
                    continue

                victim_options: list[tuple[Task, set[int]]] = []
                for victim in eligible:
                    victim_indexes = {
                        index
                        for index in victim.assigned_gpus
                        if index in state_by_index
                        and state_by_index[index].numa_node == numa_node
                        and self._gpu_can_satisfy_after_release(state_by_index[index], task)
                    }
                    if not victim_indexes:
                        continue
                    victim_options.append((victim, victim_indexes))

                for victim_count in range(1, len(victim_options) + 1):
                    for selected_options in combinations(victim_options, victim_count):
                        selected = [victim for victim, _ in selected_options]
                        releasable_on_numa = set(free_on_numa)
                        for _, victim_indexes in selected_options:
                            releasable_on_numa.update(victim_indexes)
                        if len(releasable_on_numa) < task.requested_gpus:
                            continue

                        released_slots = sum(len(victim.assigned_gpus) for victim in selected)
                        remaining_slots = len(releasable_on_numa)
                        if worker.max_concurrent_tasks is not None:
                            remaining_slots = max(
                                0,
                                worker.max_concurrent_tasks - used_gpu_slots + released_slots,
                            )
                        if remaining_slots < task.requested_gpus:
                            continue

                        regular_victims = sum(not victim.background for victim in selected)
                        released_gpu_count = sum(len(victim.assigned_gpus) for victim in selected)
                        priority_sum = sum(victim.priority for victim in selected)
                        oldest_start = min((victim.started_at or "" for victim in selected), default="")
                        candidates.append((
                            (
                                regular_victims,
                                len(selected),
                                released_gpu_count,
                                priority_sum,
                                oldest_start,
                                worker_order.get(worker.name, 0),
                                numa_node,
                            ),
                            NumaPreemptionPlan(
                                worker=worker,
                                numa_node=numa_node,
                                victims=selected,
                            ),
                        ))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def _gpu_can_satisfy_after_release(self, gpu: GpuState, task: Task) -> bool:
        if task.min_free_memory_mb is not None and gpu.memory_total_mb < task.min_free_memory_mb:
            return False
        return True

    def _apply_numa_preemption(self, task: Task, plan: NumaPreemptionPlan) -> bool:
        victim_ids = ", ".join(victim.id[:8] for victim in plan.victims)
        self.store.record_event(
            task.id,
            "numa_preempt_requested",
            f"{plan.worker.name}: numa={plan.numa_node} victims={victim_ids}",
        )
        preempted = False
        for victim in plan.victims:
            if victim.tmux_session:
                try:
                    self._kill_task(plan.worker, victim)
                except Exception as exc:
                    self.store.record_event(victim.id, "numa_preempt_kill_failed", str(exc))
                    continue
            message = (
                f"preempted by task {task.id} priority={task.priority} "
                f"for NUMA-local allocation on node {plan.numa_node}"
            )
            if self._preempt_task(victim, message):
                preempted = True
                LOGGER.info(
                    "task %s preempted by %s for NUMA node %s",
                    victim.id,
                    task.id,
                    plan.numa_node,
                )
        return preempted

    def _resize_elastic_background_tasks(self, worker_states: dict[str, list[GpuState]]) -> None:
        for task in self.store.running_tasks():
            if not task.background or not task.elastic or not task.assigned_worker:
                continue
            worker = self.workers.get(task.assigned_worker)
            if not worker or not worker.enabled:
                continue
            states = worker_states.get(worker.name, [])
            if not states:
                continue

            available = self._available_gpus_by_worker(
                task,
                worker_states,
                exclude_task_id=task.id,
            ).get(worker.name, [])
            current_indexes = list(dict.fromkeys(task.assigned_gpus))
            current = set(current_indexes)
            additional = [gpu.index for gpu in available if gpu.index not in current]

            limit = self._elastic_gpu_limit(task, worker, states)
            state_by_index = {gpu.index: gpu for gpu in states}
            if len(current_indexes) > limit:
                target_count = limit
                candidate_indexes = set(current_indexes)
            elif len(current_indexes) < limit and additional:
                target_count = min(limit, len(current_indexes) + len(additional))
                candidate_indexes = set(current_indexes) | set(additional)
            else:
                continue
            candidates = [
                state_by_index[index]
                for index in candidate_indexes
                if index in state_by_index
            ]
            selected = [gpu.index for gpu in self._select_gpu_set(candidates, target_count)]
            desired = [index for index in current_indexes if index in selected]
            desired.extend(index for index in selected if index not in current)
            if not desired:
                try:
                    if task.tmux_session:
                        self._kill_task(worker, task)
                except Exception as exc:
                    self.store.record_event(task.id, "elastic_suspend_kill_failed", str(exc))
                    continue
                message = (
                    "elastic suspended: worker has no GPU capacity available"
                )
                if self._preempt_task(task, message):
                    self.store.record_event(task.id, "elastic_suspended", message)
                continue
            if desired == current_indexes:
                continue

            try:
                if task.tmux_session:
                    self._kill_task(worker, task)
            except Exception as exc:
                self.store.record_event(task.id, "elastic_resize_kill_failed", str(exc))
                continue

            message = (
                "elastic resize "
                f"{','.join(map(str, task.assigned_gpus))} -> {','.join(map(str, desired))}"
            )
            if not self._preempt_task(task, message):
                continue
            self.store.record_event(task.id, "elastic_resize", message)
            self._start_with_requeue_on_failure(
                self.store.get_task(task.id) or task,
                DispatchPlan(worker=worker, gpu_indexes=desired),
            )

    def _preempt_for_task(
        self,
        task: Task,
        worker_states: dict[str, list[GpuState]],
    ) -> WorkerConfig | None:
        for worker in self.config.workers:
            if not worker.enabled or worker.name not in worker_states:
                continue
            if task.target_worker and worker.name != task.target_worker:
                continue

            available = self._available_gpus_by_worker(task, worker_states).get(worker.name, [])
            running_tasks = self.store.running_tasks_for_worker(worker.name)
            used_gpu_slots = sum(len(running.assigned_gpus) for running in running_tasks)
            available_slots = (
                len(available)
                if worker.max_concurrent_tasks is None
                else max(0, worker.max_concurrent_tasks - used_gpu_slots)
            )
            candidate_tasks = [
                running
                for running in running_tasks
                if not running.background and running.preemptible and running.priority < task.priority
            ]
            candidate_tasks.sort(key=lambda item: (item.priority, item.started_at or ""))

            releasable = len(available)
            releasable_slots = available_slots
            victims: list[Task] = []
            for running in candidate_tasks:
                victims.append(running)
                releasable += len(running.assigned_gpus)
                releasable_slots += len(running.assigned_gpus)
                if releasable >= task.requested_gpus and releasable_slots >= task.requested_gpus:
                    break

            if releasable < task.requested_gpus or releasable_slots < task.requested_gpus:
                continue

            victim_ids = ", ".join(victim.id[:8] for victim in victims)
            self.store.record_event(task.id, "preempt_requested", f"{worker.name}: {victim_ids}")
            preempted = False
            for victim in victims:
                if victim.tmux_session:
                    try:
                        self._kill_task(worker, victim)
                    except Exception as exc:
                        self.store.record_event(victim.id, "preempt_kill_failed", str(exc))
                        continue
                if self._preempt_task(victim, f"preempted by task {task.id} priority={task.priority}"):
                    preempted = True
                    LOGGER.info("task %s preempted by %s", victim.id, task.id)
            if preempted:
                return worker
        return None

    def _preempt_background_for_task(
        self,
        task: Task,
        worker_states: dict[str, list[GpuState]],
    ) -> WorkerConfig | None:
        for worker in self.config.workers:
            if not worker.enabled or worker.name not in worker_states:
                continue
            if task.target_worker and worker.name != task.target_worker:
                continue

            running_tasks = self.store.running_tasks_for_worker(worker.name)
            victims = [
                running
                for running in running_tasks
                if running.background and running.priority < task.priority
            ]
            victims.sort(key=lambda item: (item.priority, item.started_at or ""))

            available = self._available_gpus_by_worker(task, worker_states).get(worker.name, [])
            releasable = len(available)
            used_gpu_slots = sum(len(running.assigned_gpus) for running in running_tasks)
            releasable_slots = (
                len(available)
                if worker.max_concurrent_tasks is None
                else max(0, worker.max_concurrent_tasks - used_gpu_slots)
            )
            selected: list[Task] = []
            for running in victims:
                selected.append(running)
                releasable += len(running.assigned_gpus)
                releasable_slots += len(running.assigned_gpus)
                if releasable >= task.requested_gpus and releasable_slots >= task.requested_gpus:
                    break
            if releasable < task.requested_gpus or releasable_slots < task.requested_gpus:
                continue

            victim_ids = ", ".join(victim.id[:8] for victim in selected)
            self.store.record_event(task.id, "background_preempt_requested", f"{worker.name}: {victim_ids}")
            preempted = False
            for victim in selected:
                if victim.tmux_session:
                    try:
                        self._kill_task(worker, victim)
                    except Exception as exc:
                        self.store.record_event(victim.id, "background_preempt_kill_failed", str(exc))
                        continue
                if self._preempt_task(victim, f"preempted by task {task.id} priority={task.priority}"):
                    preempted = True
                    LOGGER.info("background task %s preempted by %s", victim.id, task.id)
            if preempted:
                return worker
        return None

    def _preempt_task(self, task: Task, message: str) -> bool:
        changed = self.store.preempt_task(task.id, message)
        if changed:
            self._run_task_notifications(task, "switch", message)
        return changed

    def _running_gpu_slots_by_worker(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.store.running_tasks():
            if task.assigned_worker:
                counts[task.assigned_worker] = counts.get(task.assigned_worker, 0) + len(task.assigned_gpus)
        return counts

    def _running_background_counts_by_worker(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.store.running_tasks():
            if task.assigned_worker and task.background:
                counts[task.assigned_worker] = counts.get(task.assigned_worker, 0) + 1
        return counts

    def _worker_has_pending_regular_task(self, worker: WorkerConfig, background_task: Task) -> bool:
        for task in self.store.queued_tasks():
            if task.background:
                continue
            if task.priority <= background_task.priority:
                continue
            if task.target_worker and task.target_worker != worker.name:
                continue
            return True
        return False

    def _target_gpu_count(self, task: Task, worker: WorkerConfig, free: list[GpuState]) -> int:
        if not task.elastic:
            return task.requested_gpus if len(free) >= task.requested_gpus else 0
        limit = self._elastic_gpu_limit(task, worker, free)
        return min(len(free), limit)

    def _sort_gpus_by_preference(self, gpus: list[GpuState]) -> list[GpuState]:
        return sorted(gpus, key=self._gpu_preference_key)

    def _select_gpu_set(self, gpus: list[GpuState], count: int) -> list[GpuState]:
        ordered = self._sort_gpus_by_preference(gpus)
        if count <= 1 or len(ordered) <= count:
            return ordered[:count]

        by_numa: dict[int, list[GpuState]] = {}
        for gpu in ordered:
            if gpu.numa_node is not None:
                by_numa.setdefault(gpu.numa_node, []).append(gpu)
        same_numa = [
            group[:count]
            for group in by_numa.values()
            if len(group) >= count
        ]
        if same_numa:
            return min(same_numa, key=self._gpu_set_load_key)
        return ordered[:count]

    def _gpu_preference_key(self, gpu: GpuState) -> tuple[int, int, int]:
        return (gpu.utilization_percent, gpu.memory_used_mb, gpu.index)

    def _gpu_set_load_key(self, gpus: list[GpuState]) -> tuple[float, int, int, int]:
        if not gpus:
            return (float("inf"), 10**9, 10**9, 10**9)
        total_utilization = sum(gpu.utilization_percent for gpu in gpus)
        max_utilization = max(gpu.utilization_percent for gpu in gpus)
        total_memory_used = sum(gpu.memory_used_mb for gpu in gpus)
        first_gpu = min(gpu.index for gpu in gpus)
        return (
            total_utilization / len(gpus),
            max_utilization,
            total_memory_used,
            first_gpu,
        )

    def _plan_preference_key(
        self,
        gpus: list[GpuState],
        worker_order: int,
    ) -> tuple[int, float, int, int, int, int]:
        numa_nodes = {gpu.numa_node for gpu in gpus if gpu.numa_node is not None}
        all_numa_known = all(gpu.numa_node is not None for gpu in gpus)
        numa_penalty = int(len(gpus) > 1 and (not all_numa_known or len(numa_nodes) != 1))
        load_key = self._gpu_set_load_key(gpus)
        return (numa_penalty, *load_key[:3], worker_order, load_key[3])

    def _elastic_gpu_limit(self, task: Task, worker: WorkerConfig, states: list[GpuState]) -> int:
        configured = min(task.requested_gpus, len(worker.gpus) if worker.gpus else len(states))
        if configured <= 0:
            configured = len(states)
        if worker.max_concurrent_tasks is not None:
            used_by_other_tasks = sum(
                len(running.assigned_gpus)
                for running in self.store.running_tasks_for_worker(worker.name)
                if running.id != task.id
            )
            remaining_gpu_slots = max(0, worker.max_concurrent_tasks - used_by_other_tasks)
            configured = min(configured, remaining_gpu_slots)
        return configured

    def _conditions_for_tasks(self, tasks: list[Task]) -> list[GpuCondition]:
        default = self._default_condition()
        conditions = {default.key: default}
        for task in tasks:
            condition = self._condition_for_task(task)
            conditions[condition.key] = condition
        return list(conditions.values())

    def _condition_for_task(self, task: Task) -> GpuCondition:
        return GpuCondition(
            max_memory_used_mb=(
                task.max_memory_used_mb
                if task.max_memory_used_mb is not None
                else 999999
                if task.min_free_memory_mb is not None
                else self.config.daemon.free_memory_mb
            ),
            max_utilization_percent=(
                task.max_utilization_percent
                if task.max_utilization_percent is not None
                else self.config.daemon.free_utilization_percent
            ),
            min_free_memory_mb=task.min_free_memory_mb,
        )

    def _default_condition(self) -> GpuCondition:
        return GpuCondition(
            max_memory_used_mb=self.config.daemon.free_memory_mb,
            max_utilization_percent=self.config.daemon.free_utilization_percent,
        )

    def _gpu_satisfies_task(self, worker_name: str, gpu: GpuState, task: Task) -> bool:
        condition = self._condition_for_task(task)
        if not gpu.is_free(
            condition.max_memory_used_mb,
            condition.max_utilization_percent,
            condition.min_free_memory_mb,
        ):
            return False
        if task.min_free_seconds <= 0:
            return True
        state = self.store.gpu_condition_state(worker_name, gpu.index, condition)
        if not state or not state.get("free_since"):
            return False
        return _elapsed_seconds(str(state["free_since"])) >= task.min_free_seconds

    def _start_with_requeue_on_failure(self, task: Task, plan: DispatchPlan) -> str | None:
        try:
            session = self.remote.start_task(plan.worker, task, plan.gpu_indexes)
            marked = self.store.mark_running(
                task.id,
                worker_name=plan.worker.name,
                gpu_indexes=plan.gpu_indexes,
                tmux_session=session,
            )
            if not marked:
                self.remote.kill_session(plan.worker, session)
                return None
            LOGGER.info(
                "task %s started on %s with GPUs %s",
                task.id,
                plan.worker.name,
                ",".join(map(str, plan.gpu_indexes)),
            )
            return session
        except Exception as exc:
            message = f"{plan.worker.name}: {exc}"
            LOGGER.warning("task %s start failed: %s", task.id, message)
            self.store.record_event(task.id, "start_attempt_failed", message)
            return None

    def _run_task_notifications(self, task: Task, event: str, detail: str | None = None) -> None:
        self._run_callback(task, event, detail)
        self._run_im_notify(task, event, detail)

    def _run_callback(self, task: Task, event: str, detail: str | None = None) -> None:
        try:
            result = run_task_callback(task, event, detail)
        except TimeoutExpired:
            self.store.record_event(task.id, "callback_failed", f"{event}: timeout")
            return
        except Exception as exc:
            self.store.record_event(task.id, "callback_failed", f"{event}: {exc}")
            return
        if result is None:
            return
        if result.returncode == 0:
            self.store.record_event(task.id, "callback", f"{event}: ok")
            return
        stderr = result.stderr.strip()[:500] or result.stdout.strip()[:500] or "no output"
        self.store.record_event(task.id, "callback_failed", f"{event}: rc={result.returncode} {stderr}")

    def _run_im_notify(self, task: Task, event: str, detail: str | None = None) -> None:
        if not task.im_notify:
            return
        try:
            result = send_task_im_notification(self.config.im_notify, task, event, detail)
        except Exception as exc:
            self.store.record_event(task.id, "im_notify_failed", f"{event}: {exc}")
            return
        self.store.record_event(
            task.id,
            "im_notify",
            f"{event}: ok {result.target_type}={result.target_id}",
        )


def _elapsed_seconds(iso_timestamp: str) -> float:
    timestamp = datetime.fromisoformat(iso_timestamp)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return (datetime.now(UTC) - timestamp).total_seconds()
