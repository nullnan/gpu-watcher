from __future__ import annotations

import json
import sqlite3
import threading
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .callbacks import normalize_callback_events
from .cron import validate_cron_expression
from .models import LOG_MODES, GpuCondition, GpuState, Task, TaskStatus, WorkerConfig
from .timeutil import utc_now


class Store:
    def __init__(self, path: str | Path):
        self.path = str(Path(path).expanduser())
        self._pool_lock = threading.Lock()
        self._idle: list[sqlite3.Connection] = []
        self._closed = False
        self._finalizer = weakref.finalize(self, self._close_idle, self._idle)

    @staticmethod
    def _close_idle(connections: list[sqlite3.Connection]) -> None:
        while connections:
            connections.pop().close()

    def close(self) -> None:
        """Close idle connections; outstanding operations close theirs on return."""
        with self._pool_lock:
            self._closed = True
            self._finalizer()

    def _acquire(self) -> sqlite3.Connection:
        with self._pool_lock:
            if self._closed:
                raise RuntimeError("Store is closed")
            if self._idle:
                return self._idle.pop()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # A connection is exclusively borrowed by one operation at a time,
        # but may be reused by a different API/scheduler thread afterwards.
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = self._acquire()
        reusable = False
        try:
            yield conn
            conn.commit()
            reusable = True
        except BaseException:
            conn.rollback()
            reusable = True
            raise
        finally:
            with self._pool_lock:
                # Keeping one idle connection prevents SQLite from deleting
                # and recreating WAL/SHM files between polling operations.
                if reusable and not self._closed and not self._idle:
                    self._idle.append(conn)
                else:
                    conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workers (
                    name TEXT PRIMARY KEY,
                    host TEXT NOT NULL,
                    user TEXT,
                    port INTEGER NOT NULL,
                    ssh_key TEXT,
                    enabled INTEGER NOT NULL,
                    tmux_prefix TEXT NOT NULL,
                    gpus_json TEXT NOT NULL,
                    max_concurrent_tasks INTEGER,
                    max_background_tasks INTEGER NOT NULL DEFAULT 1,
                    connection_state TEXT NOT NULL DEFAULT 'unknown',
                    last_probe_at TEXT,
                    last_probe_error TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    command TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    requested_gpus INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    env_json TEXT NOT NULL,
                    cwd TEXT,
                    target_worker TEXT,
                    max_memory_used_mb INTEGER,
                    min_free_memory_mb INTEGER,
                    max_utilization_percent INTEGER,
                    min_free_seconds INTEGER NOT NULL DEFAULT 0,
                    preemptible INTEGER NOT NULL DEFAULT 0,
                    allow_preempt INTEGER NOT NULL DEFAULT 0,
                    background INTEGER NOT NULL DEFAULT 0,
                    elastic INTEGER NOT NULL DEFAULT 0,
                    log_mode TEXT NOT NULL DEFAULT 'pty',
                    im_notify INTEGER NOT NULL DEFAULT 0,
                    callback_command TEXT,
                    callback_events_json TEXT NOT NULL DEFAULT '[]',
                    start_cron TEXT,
                    stop_cron TEXT,
                    schedule_timezone TEXT NOT NULL DEFAULT 'UTC',
                    last_start_schedule_at TEXT,
                    last_stop_schedule_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    assigned_worker TEXT,
                    assigned_gpus_json TEXT NOT NULL,
                    tmux_session TEXT,
                    exit_code INTEGER,
                    message TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_queue
                    ON tasks(status, priority DESC, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_tasks_worker
                    ON tasks(status, assigned_worker);

                CREATE TABLE IF NOT EXISTS task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT,
                    FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS gpu_condition_states (
                    worker_name TEXT NOT NULL,
                    gpu_index INTEGER NOT NULL,
                    condition_key TEXT NOT NULL,
                    max_memory_used_mb INTEGER NOT NULL,
                    min_free_memory_mb INTEGER,
                    max_utilization_percent INTEGER NOT NULL,
                    is_free INTEGER NOT NULL,
                    free_since TEXT,
                    last_seen TEXT NOT NULL,
                    memory_used_mb INTEGER NOT NULL,
                    memory_total_mb INTEGER NOT NULL DEFAULT 0,
                    utilization_percent INTEGER NOT NULL,
                    numa_node INTEGER,
                    process_users_json TEXT NOT NULL DEFAULT '[]',
                    processes_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(worker_name, gpu_index, condition_key)
                );
                """
            )
            self._ensure_worker_columns(conn)
            self._ensure_task_columns(conn)
            self._ensure_gpu_condition_columns(conn)

    def _ensure_worker_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(workers)").fetchall()}
        additions = {
            "max_concurrent_tasks": "ALTER TABLE workers ADD COLUMN max_concurrent_tasks INTEGER",
            "max_background_tasks": "ALTER TABLE workers ADD COLUMN max_background_tasks INTEGER NOT NULL DEFAULT 1",
            "connection_state": "ALTER TABLE workers ADD COLUMN connection_state TEXT NOT NULL DEFAULT 'unknown'",
            "last_probe_at": "ALTER TABLE workers ADD COLUMN last_probe_at TEXT",
            "last_probe_error": "ALTER TABLE workers ADD COLUMN last_probe_error TEXT",
        }
        for column, sql in additions.items():
            if column not in columns:
                conn.execute(sql)

    def _ensure_gpu_condition_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(gpu_condition_states)").fetchall()
        }
        if "process_users_json" not in columns:
            conn.execute(
                "ALTER TABLE gpu_condition_states "
                "ADD COLUMN process_users_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "processes_json" not in columns:
            conn.execute(
                "ALTER TABLE gpu_condition_states "
                "ADD COLUMN processes_json TEXT NOT NULL DEFAULT '[]'"
            )
        if "numa_node" not in columns:
            conn.execute("ALTER TABLE gpu_condition_states ADD COLUMN numa_node INTEGER")

    def _ensure_task_columns(self, conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)").fetchall()}
        additions = {
            "max_memory_used_mb": "ALTER TABLE tasks ADD COLUMN max_memory_used_mb INTEGER",
            "min_free_memory_mb": "ALTER TABLE tasks ADD COLUMN min_free_memory_mb INTEGER",
            "max_utilization_percent": "ALTER TABLE tasks ADD COLUMN max_utilization_percent INTEGER",
            "min_free_seconds": "ALTER TABLE tasks ADD COLUMN min_free_seconds INTEGER NOT NULL DEFAULT 0",
            "preemptible": "ALTER TABLE tasks ADD COLUMN preemptible INTEGER NOT NULL DEFAULT 0",
            "allow_preempt": "ALTER TABLE tasks ADD COLUMN allow_preempt INTEGER NOT NULL DEFAULT 0",
            "target_worker": "ALTER TABLE tasks ADD COLUMN target_worker TEXT",
            "background": "ALTER TABLE tasks ADD COLUMN background INTEGER NOT NULL DEFAULT 0",
            "elastic": "ALTER TABLE tasks ADD COLUMN elastic INTEGER NOT NULL DEFAULT 0",
            "log_mode": "ALTER TABLE tasks ADD COLUMN log_mode TEXT NOT NULL DEFAULT 'pty'",
            "im_notify": "ALTER TABLE tasks ADD COLUMN im_notify INTEGER NOT NULL DEFAULT 0",
            "callback_command": "ALTER TABLE tasks ADD COLUMN callback_command TEXT",
            "callback_events_json": "ALTER TABLE tasks ADD COLUMN callback_events_json TEXT NOT NULL DEFAULT '[]'",
            "start_cron": "ALTER TABLE tasks ADD COLUMN start_cron TEXT",
            "stop_cron": "ALTER TABLE tasks ADD COLUMN stop_cron TEXT",
            "schedule_timezone": "ALTER TABLE tasks ADD COLUMN schedule_timezone TEXT NOT NULL DEFAULT 'UTC'",
            "last_start_schedule_at": "ALTER TABLE tasks ADD COLUMN last_start_schedule_at TEXT",
            "last_stop_schedule_at": "ALTER TABLE tasks ADD COLUMN last_stop_schedule_at TEXT",
        }
        for column, sql in additions.items():
            if column not in columns:
                conn.execute(sql)
        gpu_columns = {row["name"] for row in conn.execute("PRAGMA table_info(gpu_condition_states)").fetchall()}
        gpu_additions = {
            "min_free_memory_mb": "ALTER TABLE gpu_condition_states ADD COLUMN min_free_memory_mb INTEGER",
            "memory_total_mb": "ALTER TABLE gpu_condition_states ADD COLUMN memory_total_mb INTEGER NOT NULL DEFAULT 0",
        }
        for column, sql in gpu_additions.items():
            if column not in gpu_columns:
                conn.execute(sql)

    def sync_workers(self, workers: list[WorkerConfig]) -> None:
        now = utc_now()
        with self.connect() as conn:
            for worker in workers:
                conn.execute(
                    """
                    INSERT INTO workers (
                        name, host, user, port, ssh_key, enabled, tmux_prefix,
                        gpus_json, max_concurrent_tasks, max_background_tasks, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(name) DO UPDATE SET
                        host = excluded.host,
                        user = excluded.user,
                        port = excluded.port,
                        ssh_key = excluded.ssh_key,
                        enabled = excluded.enabled,
                        tmux_prefix = excluded.tmux_prefix,
                        gpus_json = excluded.gpus_json,
                        max_concurrent_tasks = excluded.max_concurrent_tasks,
                        max_background_tasks = excluded.max_background_tasks,
                        updated_at = excluded.updated_at
                    """,
                    (
                        worker.name,
                        worker.host,
                        worker.user,
                        worker.port,
                        worker.ssh_key,
                        int(worker.enabled),
                        worker.tmux_prefix,
                        json.dumps(worker.gpus),
                        worker.max_concurrent_tasks,
                        worker.max_background_tasks,
                        now,
                    ),
                )

    def record_worker_probe(
        self,
        worker_name: str,
        *,
        reachable: bool,
        error: str | None = None,
    ) -> str | None:
        """Persist the latest worker connectivity result for API/UI consumers."""
        now = utc_now()
        state = "online" if reachable else "offline"
        detail = None if reachable else (error or "worker probe failed").strip()[:1000]
        with self.connect() as conn:
            row = conn.execute(
                "SELECT connection_state FROM workers WHERE name = ?",
                (worker_name,),
            ).fetchone()
            conn.execute(
                """
                UPDATE workers
                SET connection_state = ?, last_probe_at = ?, last_probe_error = ?
                WHERE name = ?
                """,
                (state, now, detail, worker_name),
            )
        return str(row["connection_state"]) if row else None

    def worker_healths(self) -> dict[str, dict[str, str | None]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT name, connection_state, last_probe_at, last_probe_error
                FROM workers
                """
            ).fetchall()
        return {
            str(row["name"]): {
                "connection_state": str(row["connection_state"] or "unknown"),
                "last_probe_at": row["last_probe_at"],
                "last_probe_error": row["last_probe_error"],
            }
            for row in rows
        }

    def create_task(
        self,
        *,
        name: str,
        command: str,
        priority: int = 0,
        requested_gpus: int = 1,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        target_worker: str | None = None,
        max_memory_used_mb: int | None = None,
        min_free_memory_mb: int | None = None,
        max_utilization_percent: int | None = None,
        min_free_seconds: int = 0,
        preemptible: bool = False,
        allow_preempt: bool = False,
        background: bool = False,
        elastic: bool = False,
        log_mode: str = "pty",
        im_notify: bool = False,
        callback_command: str | None = None,
        callback_events: list[str] | None = None,
        start_cron: str | None = None,
        stop_cron: str | None = None,
        schedule_timezone: str = "UTC",
        paused: bool = False,
    ) -> Task:
        _validate_task_options(
            command=command,
            requested_gpus=requested_gpus,
            max_memory_used_mb=max_memory_used_mb,
            min_free_memory_mb=min_free_memory_mb,
            max_utilization_percent=max_utilization_percent,
            min_free_seconds=min_free_seconds,
            log_mode=log_mode,
            start_cron=start_cron,
            stop_cron=stop_cron,
            schedule_timezone=schedule_timezone,
        )
        start_cron = _normalize_cron(start_cron)
        stop_cron = _normalize_cron(stop_cron)
        schedule_timezone = _normalize_schedule_timezone(schedule_timezone)
        callback_command = callback_command.strip() if callback_command else None
        normalized_callback_events = normalize_callback_events(callback_events) if callback_command else []
        task_id = str(uuid.uuid4())
        now = utc_now()
        status = TaskStatus.PAUSED.value if paused else TaskStatus.QUEUED.value
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, name, command, priority, requested_gpus, status,
                    env_json, cwd, target_worker,
                    max_memory_used_mb, min_free_memory_mb, max_utilization_percent,
                    min_free_seconds, preemptible, allow_preempt, background, elastic,
                    log_mode, im_notify, callback_command, callback_events_json,
                    start_cron, stop_cron, schedule_timezone,
                    created_at, updated_at, assigned_gpus_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    name,
                    command,
                    int(priority),
                    int(requested_gpus),
                    status,
                    json.dumps(env or {}, sort_keys=True),
                    cwd,
                    target_worker,
                    max_memory_used_mb,
                    min_free_memory_mb,
                    max_utilization_percent,
                    int(min_free_seconds),
                    int(preemptible or background),
                    int(allow_preempt),
                    int(background),
                    int(elastic),
                    log_mode,
                    int(im_notify),
                    callback_command,
                    json.dumps(normalized_callback_events),
                    start_cron,
                    stop_cron,
                    schedule_timezone,
                    now,
                    now,
                    "[]",
                ),
            )
            self._insert_event(conn, task_id, "paused" if paused else "queued", "created paused" if paused else None)
        task = self.get_task(task_id)
        assert task is not None
        return task

    def update_task(
        self,
        task_id: str,
        *,
        name: str,
        command: str,
        priority: int,
        requested_gpus: int,
        env: dict[str, str],
        cwd: str | None,
        target_worker: str | None,
        max_memory_used_mb: int | None,
        min_free_memory_mb: int | None,
        max_utilization_percent: int | None,
        min_free_seconds: int,
        preemptible: bool,
        allow_preempt: bool,
        background: bool,
        elastic: bool,
        log_mode: str,
        im_notify: bool,
        callback_command: str | None,
        callback_events: list[str] | None,
        start_cron: str | None = None,
        stop_cron: str | None = None,
        schedule_timezone: str = "UTC",
    ) -> Task | None:
        _validate_task_options(
            command=command,
            requested_gpus=requested_gpus,
            max_memory_used_mb=max_memory_used_mb,
            min_free_memory_mb=min_free_memory_mb,
            max_utilization_percent=max_utilization_percent,
            min_free_seconds=min_free_seconds,
            log_mode=log_mode,
            start_cron=start_cron,
            stop_cron=stop_cron,
            schedule_timezone=schedule_timezone,
        )
        start_cron = _normalize_cron(start_cron)
        stop_cron = _normalize_cron(stop_cron)
        schedule_timezone = _normalize_schedule_timezone(schedule_timezone)
        callback_command = callback_command.strip() if callback_command else None
        normalized_callback_events = normalize_callback_events(callback_events) if callback_command else []
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET name = ?, command = ?, priority = ?, requested_gpus = ?,
                    env_json = ?, cwd = ?, target_worker = ?,
                    max_memory_used_mb = ?, min_free_memory_mb = ?,
                    max_utilization_percent = ?, min_free_seconds = ?,
                    preemptible = ?, allow_preempt = ?, background = ?, elastic = ?,
                    log_mode = ?, im_notify = ?, callback_command = ?,
                    callback_events_json = ?, start_cron = ?, stop_cron = ?,
                    schedule_timezone = ?, last_start_schedule_at = NULL,
                    last_stop_schedule_at = NULL, updated_at = ?
                WHERE id = ? AND (
                    status = ? OR (
                        status = ? AND started_at IS NULL
                        AND assigned_worker IS NULL AND tmux_session IS NULL
                    )
                )
                """,
                (
                    name,
                    command,
                    int(priority),
                    int(requested_gpus),
                    json.dumps(env, sort_keys=True),
                    cwd,
                    target_worker,
                    max_memory_used_mb,
                    min_free_memory_mb,
                    max_utilization_percent,
                    int(min_free_seconds),
                    int(preemptible or background),
                    int(allow_preempt),
                    int(background),
                    int(elastic),
                    log_mode,
                    int(im_notify),
                    callback_command,
                    json.dumps(normalized_callback_events),
                    start_cron,
                    stop_cron,
                    schedule_timezone,
                    now,
                    task_id,
                    TaskStatus.PAUSED.value,
                    TaskStatus.QUEUED.value,
                ),
            )
            if cursor.rowcount == 0:
                return None
            self._insert_event(conn, task_id, "updated", "task options updated")
        return self.get_task(task_id)

    def list_tasks(
        self,
        status: str | None = None,
        limit: int = 100,
        offset: int = 0,
        search: str | None = None,
    ) -> list[Task]:
        limit = max(1, int(limit))
        offset = max(0, int(offset))
        clauses: list[str] = []
        params: list[object] = []
        if status == "terminal":
            clauses.append("status IN (?, ?, ?)")
            params.extend(
                [
                    TaskStatus.SUCCEEDED.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELED.value,
                ]
            )
        elif status:
            clauses.append("status = ?")
            params.append(status)
        if search and (term := search.strip()):
            clauses.append("(instr(lower(name), lower(?)) > 0 OR instr(lower(id), lower(?)) > 0)")
            params.extend([term, term])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM tasks
                {where}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def count_tasks(self, status: str | None = None, search: str | None = None) -> int:
        clauses: list[str] = []
        params: list[object] = []
        if status == "terminal":
            clauses.append("status IN (?, ?, ?)")
            params.extend(
                [
                    TaskStatus.SUCCEEDED.value,
                    TaskStatus.FAILED.value,
                    TaskStatus.CANCELED.value,
                ]
            )
        elif status:
            clauses.append("status = ?")
            params.append(status)
        if search and (term := search.strip()):
            clauses.append("(instr(lower(name), lower(?)) > 0 OR instr(lower(id), lower(?)) > 0)")
            params.extend([term, term])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM tasks {where}",
                params,
            ).fetchone()
        return int(row["count"] if row else 0)

    def task_summary(self) -> dict[str, int]:
        summary = {
            TaskStatus.PAUSED.value: 0,
            TaskStatus.QUEUED.value: 0,
            TaskStatus.RUNNING.value: 0,
            TaskStatus.SUCCEEDED.value: 0,
            TaskStatus.FAILED.value: 0,
            TaskStatus.CANCELED.value: 0,
        }
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM tasks
                GROUP BY status
                """
            ).fetchall()
        for row in rows:
            summary[str(row["status"])] = int(row["count"])
        summary["terminal"] = (
            summary[TaskStatus.SUCCEEDED.value]
            + summary[TaskStatus.FAILED.value]
            + summary[TaskStatus.CANCELED.value]
        )
        summary["all"] = sum(
            summary[status.value]
            for status in TaskStatus
        )
        return summary

    def queued_tasks(self, limit: int = 100) -> list[Task]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = ?
                ORDER BY priority DESC, created_at ASC
                LIMIT ?
                """,
                (TaskStatus.QUEUED.value, limit),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def running_tasks(self) -> list[Task]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY started_at ASC",
                (TaskStatus.RUNNING.value,),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def running_tasks_for_worker(self, worker_name: str) -> list[Task]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE status = ? AND assigned_worker = ?
                ORDER BY priority ASC, started_at ASC
                """,
                (TaskStatus.RUNNING.value, worker_name),
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def get_task(self, task_id: str) -> Task | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return _row_to_task(row) if row else None

    def scheduled_tasks(self) -> list[Task]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                WHERE start_cron IS NOT NULL OR stop_cron IS NOT NULL
                ORDER BY created_at ASC
                """
            ).fetchall()
        return [_row_to_task(row) for row in rows]

    def claim_schedule_trigger(self, task_id: str, action: str, minute_key: str) -> bool:
        """Record one cron firing, allowing at most one action per UTC minute."""
        if action not in {"start", "stop"}:
            raise ValueError("schedule action must be 'start' or 'stop'")
        column = "last_start_schedule_at" if action == "start" else "last_stop_schedule_at"
        with self.connect() as conn:
            cursor = conn.execute(
                f"""
                UPDATE tasks
                SET {column} = ?, updated_at = updated_at
                WHERE id = ? AND ({column} IS NULL OR {column} != ?)
                """,
                (minute_key, task_id, minute_key),
            )
        return cursor.rowcount > 0

    def mark_running(
        self,
        task_id: str,
        *,
        worker_name: str,
        gpu_indexes: list[int],
        tmux_session: str,
    ) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, started_at = ?,
                    assigned_worker = ?, assigned_gpus_json = ?, tmux_session = ?,
                    message = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.RUNNING.value,
                    now,
                    now,
                    worker_name,
                    json.dumps(gpu_indexes),
                    tmux_session,
                    task_id,
                    TaskStatus.QUEUED.value,
                ),
            )
            changed = cursor.rowcount > 0
            if changed:
                self._insert_event(
                    conn,
                    task_id,
                    "started",
                    f"{worker_name} gpus={','.join(map(str, gpu_indexes))} session={tmux_session}",
                )
        return changed

    def finish_task(self, task_id: str, *, exit_code: int, message: str | None = None) -> None:
        status = TaskStatus.SUCCEEDED if exit_code == 0 else TaskStatus.FAILED
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, finished_at = ?, exit_code = ?, message = ?
                WHERE id = ? AND status = ?
                """,
                (
                    status.value,
                    now,
                    now,
                    exit_code,
                    message,
                    task_id,
                    TaskStatus.RUNNING.value,
                ),
            )
            self._insert_event(conn, task_id, status.value, message)

    def fail_task(self, task_id: str, *, message: str) -> None:
        """Mark a running task failed when no reliable process exit code exists."""
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, finished_at = ?, exit_code = NULL, message = ?
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.FAILED.value,
                    now,
                    now,
                    message,
                    task_id,
                    TaskStatus.RUNNING.value,
                ),
            )
            self._insert_event(conn, task_id, TaskStatus.FAILED.value, message)

    def fail_start(self, task_id: str, message: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, finished_at = ?, message = ?
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.FAILED.value,
                    now,
                    now,
                    message,
                    task_id,
                    TaskStatus.QUEUED.value,
                ),
            )
            self._insert_event(conn, task_id, "start_failed", message)

    def requeue_task(self, task_id: str, message: str) -> None:
        now = utc_now()
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, message = ?,
                    assigned_worker = NULL, assigned_gpus_json = '[]',
                    tmux_session = NULL, started_at = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.QUEUED.value,
                    now,
                    message,
                    task_id,
                    TaskStatus.RUNNING.value,
                ),
            )
            self._insert_event(conn, task_id, "requeued", message)

    def start_task(
        self,
        task_id: str,
        message: str = "manual start requested",
        event: str = "start_requested",
    ) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, message = ?,
                    finished_at = NULL, exit_code = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.QUEUED.value,
                    now,
                    message,
                    task_id,
                    TaskStatus.PAUSED.value,
                ),
            )
            changed = cursor.rowcount > 0
            if changed:
                self._insert_event(conn, task_id, event, message)
        return changed

    def pause_task(
        self,
        task_id: str,
        message: str = "paused by user",
        *,
        require_preemptible: bool = True,
        event: str = "paused",
    ) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, message = ?,
                    assigned_worker = NULL, assigned_gpus_json = '[]',
                    tmux_session = NULL, started_at = NULL,
                    finished_at = NULL, exit_code = NULL
                WHERE id = ? AND status IN (?, ?)
                  AND (? = 0 OR preemptible = 1)
                """,
                (
                    TaskStatus.PAUSED.value,
                    now,
                    message,
                    task_id,
                    TaskStatus.QUEUED.value,
                    TaskStatus.RUNNING.value,
                    int(require_preemptible),
                ),
            )
            changed = cursor.rowcount > 0
            if changed:
                self._insert_event(conn, task_id, event, message)
        return changed

    def rerun_task(self, task_id: str, message: str = "rerun requested") -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, message = ?,
                    assigned_worker = NULL, assigned_gpus_json = '[]',
                    tmux_session = NULL, started_at = NULL,
                    finished_at = NULL, exit_code = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.QUEUED.value,
                    now,
                    message,
                    task_id,
                    TaskStatus.FAILED.value,
                ),
            )
            changed = cursor.rowcount > 0
            if changed:
                self._insert_event(conn, task_id, "rerun_requested", message)
        return changed

    def preempt_task(self, task_id: str, message: str) -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, message = ?,
                    assigned_worker = NULL, assigned_gpus_json = '[]',
                    tmux_session = NULL, started_at = NULL
                WHERE id = ? AND status = ?
                """,
                (
                    TaskStatus.QUEUED.value,
                    now,
                    message,
                    task_id,
                    TaskStatus.RUNNING.value,
                ),
            )
            changed = cursor.rowcount > 0
            if changed:
                self._insert_event(conn, task_id, "preempted", message)
        return changed

    def cancel_task(self, task_id: str, message: str = "terminated by user") -> bool:
        now = utc_now()
        with self.connect() as conn:
            cursor = conn.execute(
                """
                UPDATE tasks
                SET status = ?, updated_at = ?, finished_at = ?, message = ?
                WHERE id = ? AND status IN (?, ?, ?)
                """,
                (
                    TaskStatus.CANCELED.value,
                    now,
                    now,
                    message,
                    task_id,
                    TaskStatus.PAUSED.value,
                    TaskStatus.QUEUED.value,
                    TaskStatus.RUNNING.value,
                ),
            )
            changed = cursor.rowcount > 0
            if changed:
                self._insert_event(conn, task_id, "canceled", message)
        return changed

    def record_event(self, task_id: str, event: str, detail: str | None = None) -> None:
        with self.connect() as conn:
            self._insert_event(conn, task_id, event, detail)

    def task_events(self, task_id: str) -> list[dict[str, str | None]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT created_at, event, detail
                FROM task_events
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_gpu_condition_states(
        self,
        worker_name: str,
        gpu_states: list[GpuState],
        conditions: list[GpuCondition],
    ) -> None:
        now = utc_now()
        with self.connect() as conn:
            for gpu in gpu_states:
                for condition in conditions:
                    row = conn.execute(
                        """
                        SELECT is_free, free_since
                        FROM gpu_condition_states
                        WHERE worker_name = ? AND gpu_index = ? AND condition_key = ?
                        """,
                        (worker_name, gpu.index, condition.key),
                    ).fetchone()
                    is_free = gpu.is_free(
                        condition.max_memory_used_mb,
                        condition.max_utilization_percent,
                        condition.min_free_memory_mb,
                    )
                    previous_free = bool(row["is_free"]) if row else False
                    free_since = row["free_since"] if row and previous_free and is_free else None
                    if is_free and not free_since:
                        free_since = now
                    conn.execute(
                        """
                        INSERT INTO gpu_condition_states (
                            worker_name, gpu_index, condition_key,
                            max_memory_used_mb, min_free_memory_mb, max_utilization_percent,
                            is_free, free_since, last_seen,
                            memory_used_mb, memory_total_mb, utilization_percent, numa_node,
                            process_users_json, processes_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(worker_name, gpu_index, condition_key) DO UPDATE SET
                            max_memory_used_mb = excluded.max_memory_used_mb,
                            min_free_memory_mb = excluded.min_free_memory_mb,
                            max_utilization_percent = excluded.max_utilization_percent,
                            is_free = excluded.is_free,
                            free_since = excluded.free_since,
                            last_seen = excluded.last_seen,
                            memory_used_mb = excluded.memory_used_mb,
                            memory_total_mb = excluded.memory_total_mb,
                            utilization_percent = excluded.utilization_percent,
                            numa_node = excluded.numa_node,
                            process_users_json = excluded.process_users_json,
                            processes_json = excluded.processes_json
                        """,
                        (
                            worker_name,
                            gpu.index,
                            condition.key,
                            condition.max_memory_used_mb,
                            condition.min_free_memory_mb,
                            condition.max_utilization_percent,
                            int(is_free),
                            free_since,
                            now,
                            gpu.memory_used_mb,
                            gpu.memory_total_mb,
                            gpu.utilization_percent,
                            gpu.numa_node,
                            json.dumps(gpu.process_users),
                            json.dumps([process.as_dict() for process in gpu.processes]),
                        ),
                    )

    def gpu_condition_state(
        self,
        worker_name: str,
        gpu_index: int,
        condition: GpuCondition,
    ) -> dict[str, object] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM gpu_condition_states
                WHERE worker_name = ? AND gpu_index = ? AND condition_key = ?
                """,
                (worker_name, gpu_index, condition.key),
            ).fetchone()
        return _gpu_state_row(row) if row else None

    def list_gpu_condition_states(self, worker_name: str | None = None) -> list[dict[str, object]]:
        with self.connect() as conn:
            if worker_name:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM gpu_condition_states
                    WHERE worker_name = ?
                    ORDER BY worker_name, gpu_index, condition_key
                    """,
                    (worker_name,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM gpu_condition_states
                    ORDER BY worker_name, gpu_index, condition_key
                    """
                ).fetchall()
        return [_gpu_state_row(row) for row in rows]

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        event: str,
        detail: str | None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO task_events(task_id, created_at, event, detail)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, utc_now(), event, detail),
        )


def _validate_task_options(
    *,
    command: str,
    requested_gpus: int,
    max_memory_used_mb: int | None,
    min_free_memory_mb: int | None,
    max_utilization_percent: int | None,
    min_free_seconds: int,
    log_mode: str,
    start_cron: str | None = None,
    stop_cron: str | None = None,
    schedule_timezone: str = "UTC",
) -> None:
    if not command.strip():
        raise ValueError("command is required")
    if requested_gpus < 1:
        raise ValueError("requested_gpus must be at least 1")
    if max_memory_used_mb is not None and max_memory_used_mb < 0:
        raise ValueError("max_memory_used_mb cannot be negative")
    if min_free_memory_mb is not None and min_free_memory_mb < 0:
        raise ValueError("min_free_memory_mb cannot be negative")
    if max_utilization_percent is not None and not 0 <= max_utilization_percent <= 100:
        raise ValueError("max_utilization_percent must be between 0 and 100")
    if min_free_seconds < 0:
        raise ValueError("min_free_seconds cannot be negative")
    if log_mode not in LOG_MODES:
        raise ValueError(f"log_mode must be one of: {', '.join(sorted(LOG_MODES))}")
    _normalize_cron(start_cron)
    _normalize_cron(stop_cron)
    _normalize_schedule_timezone(schedule_timezone)


def _normalize_cron(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = validate_cron_expression(str(value))
    return normalized or None


def _normalize_schedule_timezone(value: str | None) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    timezone = (value or "UTC").strip() or "UTC"
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown schedule timezone: {timezone}") from exc
    return timezone


def _gpu_state_row(row: sqlite3.Row) -> dict[str, object]:
    result = dict(row)
    raw_users = result.pop("process_users_json", "[]")
    raw_processes = result.pop("processes_json", "[]")
    try:
        users = json.loads(str(raw_users or "[]"))
    except json.JSONDecodeError:
        users = []
    try:
        processes = json.loads(str(raw_processes or "[]"))
    except json.JSONDecodeError:
        processes = []
    result["process_users"] = users if isinstance(users, list) else []
    result["processes"] = processes if isinstance(processes, list) else []
    return result


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=str(row["id"]),
        name=str(row["name"]),
        command=str(row["command"]),
        priority=int(row["priority"]),
        requested_gpus=int(row["requested_gpus"]),
        status=str(row["status"]),
        env=json.loads(row["env_json"]),
        cwd=row["cwd"],
        target_worker=row["target_worker"],
        max_memory_used_mb=row["max_memory_used_mb"],
        min_free_memory_mb=row["min_free_memory_mb"],
        max_utilization_percent=row["max_utilization_percent"],
        min_free_seconds=int(row["min_free_seconds"]),
        preemptible=bool(row["preemptible"]),
        allow_preempt=bool(row["allow_preempt"]),
        background=bool(row["background"]),
        elastic=bool(row["elastic"]),
        log_mode=str(row["log_mode"]),
        im_notify=bool(row["im_notify"]),
        callback_command=row["callback_command"],
        callback_events=json.loads(row["callback_events_json"] or "[]"),
        start_cron=row["start_cron"],
        stop_cron=row["stop_cron"],
        schedule_timezone=str(row["schedule_timezone"] or "UTC"),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        assigned_worker=row["assigned_worker"],
        assigned_gpus=json.loads(row["assigned_gpus_json"]),
        tmux_session=row["tmux_session"],
        exit_code=row["exit_code"],
        message=row["message"],
    )
