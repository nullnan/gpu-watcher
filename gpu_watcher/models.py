from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class TaskStatus(StrEnum):
    PAUSED = "paused"
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_STATUSES = {
    TaskStatus.SUCCEEDED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELED.value,
}

LOG_MODES = {"pty", "pipe"}
CALLBACK_EVENTS = {"succeeded", "failed", "switch"}


@dataclass(frozen=True)
class WorkerConfig:
    name: str
    host: str
    user: str | None = None
    port: int = 22
    ssh_key: str | None = None
    enabled: bool = True
    tmux_prefix: str = "gw"
    gpus: list[int] = field(default_factory=list)
    max_concurrent_tasks: int | None = None
    max_background_tasks: int = 1

    @property
    def ssh_target(self) -> str:
        if self.user:
            return f"{self.user}@{self.host}"
        return self.host


@dataclass(frozen=True)
class DaemonConfig:
    db_path: str
    poll_interval_seconds: float = 10.0
    remote_base_dir: str = "~/.gpu-watcher"
    free_memory_mb: int = 512
    free_utilization_percent: int = 10


@dataclass(frozen=True)
class ApiConfig:
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool = False
    username: str = "admin"
    password_hash: str | None = field(default=None, repr=False)
    session_secret: str | None = field(default=None, repr=False)
    secure_cookie: bool = False
    session_ttl_seconds: int = 12 * 60 * 60
    login_max_attempts: int = 5
    login_window_seconds: int = 5 * 60
    cookie_name: str = "gpu_watcher_session"


@dataclass(frozen=True)
class ImNotifyConfig:
    enabled: bool = False
    api: str = "http://127.0.0.1:3000"
    token: str | None = None
    user: int | None = None
    group: int | None = None
    timeout_seconds: float = 5.0


@dataclass(frozen=True)
class AppConfig:
    daemon: DaemonConfig
    api: ApiConfig
    workers: list[WorkerConfig]
    im_notify: ImNotifyConfig = field(default_factory=ImNotifyConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    config_path: str | None = None


@dataclass(frozen=True)
class GpuCondition:
    max_memory_used_mb: int
    max_utilization_percent: int
    min_free_memory_mb: int | None = None

    @property
    def key(self) -> str:
        parts = [
            f"mem<={self.max_memory_used_mb}",
            f"util<={self.max_utilization_percent}",
        ]
        if self.min_free_memory_mb is not None:
            parts.append(f"free>={self.min_free_memory_mb}")
        return ";".join(parts)


@dataclass(frozen=True)
class Task:
    id: str
    name: str
    command: str
    priority: int
    requested_gpus: int
    status: str
    env: dict[str, str]
    cwd: str | None
    target_worker: str | None
    max_memory_used_mb: int | None
    min_free_memory_mb: int | None
    max_utilization_percent: int | None
    min_free_seconds: int
    preemptible: bool
    allow_preempt: bool
    background: bool
    elastic: bool
    log_mode: str
    callback_command: str | None
    callback_events: list[str]
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    assigned_worker: str | None = None
    assigned_gpus: list[int] = field(default_factory=list)
    tmux_session: str | None = None
    exit_code: int | None = None
    message: str | None = None
    im_notify: bool = False
    start_cron: str | None = None
    stop_cron: str | None = None
    schedule_timezone: str = "UTC"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "command": self.command,
            "priority": self.priority,
            "requested_gpus": self.requested_gpus,
            "status": self.status,
            "env": self.env,
            "cwd": self.cwd,
            "target_worker": self.target_worker,
            "max_memory_used_mb": self.max_memory_used_mb,
            "min_free_memory_mb": self.min_free_memory_mb,
            "max_utilization_percent": self.max_utilization_percent,
            "min_free_seconds": self.min_free_seconds,
            "preemptible": self.preemptible,
            "allow_preempt": self.allow_preempt,
            "background": self.background,
            "elastic": self.elastic,
            "log_mode": self.log_mode,
            "im_notify": self.im_notify,
            "callback_command": self.callback_command,
            "callback_events": self.callback_events,
            "start_cron": self.start_cron,
            "stop_cron": self.stop_cron,
            "schedule_timezone": self.schedule_timezone,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds(),
            "assigned_worker": self.assigned_worker,
            "assigned_gpus": self.assigned_gpus,
            "tmux_session": self.tmux_session,
            "exit_code": self.exit_code,
            "message": self.message,
        }

    def duration_seconds(self) -> int | None:
        if not self.started_at:
            return None
        started = _parse_iso_datetime(self.started_at)
        finished = _parse_iso_datetime(self.finished_at) if self.finished_at else datetime.now(UTC)
        return max(0, int((finished - started).total_seconds()))


@dataclass(frozen=True)
class GpuProcess:
    pid: int
    memory_used_mb: int
    username: str
    cmdline: str

    def as_dict(self) -> dict[str, int | str]:
        return {
            "pid": self.pid,
            "memory_used_mb": self.memory_used_mb,
            "username": self.username,
            "cmdline": self.cmdline,
        }


@dataclass(frozen=True)
class GpuState:
    index: int
    memory_used_mb: int
    utilization_percent: int
    memory_total_mb: int = 0
    numa_node: int | None = None
    process_users: tuple[str, ...] = field(default_factory=tuple)
    processes: tuple[GpuProcess, ...] = field(default_factory=tuple)

    @property
    def memory_free_mb(self) -> int | None:
        if self.memory_total_mb <= 0:
            return None
        return max(0, self.memory_total_mb - self.memory_used_mb)

    def is_free(
        self,
        memory_threshold_mb: int,
        utilization_threshold_percent: int,
        min_free_memory_mb: int | None = None,
    ) -> bool:
        if self.memory_used_mb > memory_threshold_mb:
            return False
        if self.utilization_percent > utilization_threshold_percent:
            return False
        if min_free_memory_mb is not None:
            memory_free = self.memory_free_mb
            return memory_free is not None and memory_free >= min_free_memory_mb
        return True


def _parse_iso_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
