from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass

from .models import Task

CALLBACK_EVENTS = {"succeeded", "failed", "switch"}
DEFAULT_CALLBACK_EVENTS = ("succeeded", "failed", "switch")


@dataclass(frozen=True)
class CallbackResult:
    event: str
    returncode: int
    stdout: str
    stderr: str


def normalize_callback_events(events: list[str] | tuple[str, ...] | None) -> list[str]:
    if not events:
        return list(DEFAULT_CALLBACK_EVENTS)
    normalized: list[str] = []
    for raw_event in events:
        for part in str(raw_event).split(","):
            event = _normalize_callback_event(part)
            if event not in normalized:
                normalized.append(event)
    return normalized


def run_task_callback(
    task: Task,
    event: str,
    detail: str | None = None,
    *,
    timeout_seconds: int = 30,
) -> CallbackResult | None:
    if not task.callback_command or event not in task.callback_events:
        return None
    title, content = task_event_message(task, event, detail)
    env = _callback_env(task, event, title, content, detail)
    command = _render_command(task.callback_command, env)
    completed = subprocess.run(
        command,
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
        env={**os.environ, **env},
    )
    return CallbackResult(
        event=event,
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _normalize_callback_event(event: str) -> str:
    normalized = event.strip().lower().replace("_", "-")
    aliases = {
        "complete": "succeeded",
        "completed": "succeeded",
        "success": "succeeded",
        "succeed": "succeeded",
        "succeeded": "succeeded",
        "fail": "failed",
        "failure": "failed",
        "failed": "failed",
        "switch": "switch",
        "switched": "switch",
        "resize": "switch",
        "preempt": "switch",
        "preempted": "switch",
    }
    if normalized not in aliases:
        raise ValueError(f"callback event must be one of: {', '.join(sorted(CALLBACK_EVENTS))}")
    return aliases[normalized]


def task_event_message(task: Task, event: str, detail: str | None) -> tuple[str, str]:
    title = f"gpu-watcher {event}: {task.name}"
    content_lines = [
        f"id={task.id}",
        f"event={event}",
        f"status={task.status}",
        f"worker={task.assigned_worker or '-'}",
        f"gpus={','.join(map(str, task.assigned_gpus)) or '-'}",
        f"target={task.target_worker or 'auto'}",
        f"exit_code={task.exit_code if task.exit_code is not None else '-'}",
    ]
    duration = task.duration_seconds()
    if duration is not None:
        content_lines.append(f"duration_seconds={duration}")
    if detail:
        content_lines.append(f"detail={detail}")
    return title, "\n".join(content_lines)


def _callback_env(task: Task, event: str, title: str, content: str, detail: str | None) -> dict[str, str]:
    return {
        "GPU_WATCHER_CALLBACK_EVENT": event,
        "GPU_WATCHER_CALLBACK_TITLE": title,
        "GPU_WATCHER_CALLBACK_CONTENT": content,
        "GPU_WATCHER_TASK_ID": task.id,
        "GPU_WATCHER_TASK_NAME": task.name,
        "GPU_WATCHER_TASK_STATUS": task.status,
        "GPU_WATCHER_TASK_EXIT_CODE": "" if task.exit_code is None else str(task.exit_code),
        "GPU_WATCHER_TASK_WORKER": task.assigned_worker or "",
        "GPU_WATCHER_TASK_GPUS": ",".join(map(str, task.assigned_gpus)),
        "GPU_WATCHER_TASK_TARGET_WORKER": task.target_worker or "auto",
        "GPU_WATCHER_TASK_DETAIL": detail or "",
    }


def _render_command(command: str, env: dict[str, str]) -> str:
    replacements = {
        "title": env["GPU_WATCHER_CALLBACK_TITLE"],
        "content": env["GPU_WATCHER_CALLBACK_CONTENT"],
        "event": env["GPU_WATCHER_CALLBACK_EVENT"],
        "task_id": env["GPU_WATCHER_TASK_ID"],
        "task_name": env["GPU_WATCHER_TASK_NAME"],
        "status": env["GPU_WATCHER_TASK_STATUS"],
        "exit_code": env["GPU_WATCHER_TASK_EXIT_CODE"],
        "worker": env["GPU_WATCHER_TASK_WORKER"],
        "gpus": env["GPU_WATCHER_TASK_GPUS"],
        "detail": env["GPU_WATCHER_TASK_DETAIL"],
    }
    rendered = command
    for key, value in replacements.items():
        rendered = rendered.replace("{" + key + "}", shlex.quote(value))
    return rendered
