from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from .auth import hash_password
from .config import load_config
from .daemon import run_daemon
from .remote import RemoteExecutor
from .scheduler import Scheduler
from .store import Store

DEFAULT_API_URL = "http://127.0.0.1:8765"
API_URL_ENV = "GPU_WATCHER_API_URL"
LOCAL_CLIENT_HEADER = "x-gpu-watcher-local-client"
TERMINAL_TASK_STATUSES = {"succeeded", "failed", "canceled"}
DEFAULT_WAIT_INTERVAL_SECONDS = 5.0


@dataclass(frozen=True)
class CliContext:
    db_path: str | None
    config_path: str | None
    api_url: str | None


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="gpu-watcher")
    sub = parser.add_subparsers(dest="command_name", required=True)

    daemon = sub.add_parser("daemon", help="run the central scheduler daemon")
    daemon.add_argument("--config", required=True)
    daemon.add_argument("--log-level", default="INFO")

    hash_password_parser = sub.add_parser("hash-password", help="create an Argon2id password hash for daemon authentication")

    submit = sub.add_parser("submit", help="submit a task to the queue")
    _add_connection_args(submit)
    submit.add_argument("--name", default="task")
    submit.add_argument("--priority", type=int, default=0)
    submit.add_argument("--gpus", type=int, default=1)
    submit.add_argument("--worker", "--target-worker", dest="target_worker", default="auto", help="worker name, or auto")
    submit.add_argument("--cwd")
    submit.add_argument("--env", action="append", default=[], help="KEY=VALUE, repeatable")
    submit.add_argument("--max-memory-used-mb", type=int)
    submit.add_argument("--min-free-memory-mb", type=int)
    submit.add_argument("--max-utilization-percent", type=int)
    submit.add_argument("--min-free-seconds", type=int, default=0)
    submit.add_argument("--preemptible", action="store_true", help="allow higher priority tasks to requeue this task")
    submit.add_argument("--allow-preempt", action="store_true", help="allow this task to preempt lower priority preemptible tasks")
    submit.add_argument("--background", action="store_true", help="run only in idle capacity and yield to higher priority tasks")
    submit.add_argument("--elastic", action="store_true", help="for background work, run on 1 to --gpus suitable GPUs and restart when that count changes")
    submit.add_argument("--log-mode", choices=["pty", "pipe"], default="pty", help="capture logs through a PTY or through plain stdout/stderr pipes")
    submit.add_argument("--im-notify", action="store_true", help="send built-in IM notifications for succeeded, failed, and switch events")
    submit.add_argument("--callback-command", help="shell command run by the daemon for callback events; supports {title} and {content}")
    submit.add_argument("--callback-on", action="append", default=[], help="callback event: succeeded, failed, or switch; repeatable or comma-separated")
    submit.add_argument("--start-cron", help="five-field cron expression that starts a paused task")
    submit.add_argument("--stop-cron", help="five-field cron expression that pauses a queued or running task")
    submit.add_argument("--schedule-timezone", default="UTC", help="IANA timezone for --start-cron/--stop-cron (default: UTC)")
    submit.add_argument("--paused", action="store_true", help="create the task paused; run gpu-watcher start TASK_ID to enqueue it")
    submit.add_argument("--wait", action="store_true", help="block until the submitted task reaches a terminal status")
    submit.add_argument("--wait-timeout", type=float, help="maximum seconds to wait for --wait")
    submit.add_argument("--wait-interval", type=float, default=DEFAULT_WAIT_INTERVAL_SECONDS, help="seconds between wait polls")
    submit.add_argument("task_command", nargs=argparse.REMAINDER)

    tasks = sub.add_parser("tasks", aliases=["list"], help="list tasks")
    _add_connection_args(tasks)
    tasks.add_argument("--status")
    tasks.add_argument("--limit", type=int, default=100)
    tasks.add_argument("--json", action="store_true")

    show = sub.add_parser("show", help="show one task")
    _add_connection_args(show)
    show.add_argument("task_id")
    show.add_argument("--json", action="store_true")

    events = sub.add_parser("events", help="show task events")
    _add_connection_args(events)
    events.add_argument("task_id")
    events.add_argument("--json", action="store_true")

    wait = sub.add_parser("wait", help="block until a task reaches a terminal status")
    _add_connection_args(wait)
    wait.add_argument("task_id")
    wait.add_argument("--timeout", type=float, help="maximum seconds to wait")
    wait.add_argument("--interval", type=float, default=DEFAULT_WAIT_INTERVAL_SECONDS, help="seconds between polls")
    wait.add_argument("--json", action="store_true")

    gpus = sub.add_parser("gpus", help="show last monitored GPU condition states")
    _add_connection_args(gpus)
    gpus.add_argument("--worker")
    gpus.add_argument("--json", action="store_true")

    workers = sub.add_parser("workers", help="list configured workers")
    _add_connection_args(workers)
    workers.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="show scheduler summary")
    _add_connection_args(status)
    status.add_argument("--json", action="store_true")

    start = sub.add_parser("start", help="start a paused task, or request dispatch for a queued task")
    _add_connection_args(start)
    start.add_argument("task_id")

    pause = sub.add_parser("pause", help="pause a queued or running task")
    _add_connection_args(pause)
    pause.add_argument("task_id")

    rerun = sub.add_parser("rerun", help="rerun a failed task")
    _add_connection_args(rerun)
    rerun.add_argument("task_id")

    terminate = sub.add_parser("terminate", help="terminate a queued, paused, or running task")
    _add_connection_args(terminate)
    terminate.add_argument("task_id")

    cancel = sub.add_parser("cancel", help="compatibility alias for terminate")
    _add_connection_args(cancel)
    cancel.add_argument("task_id")

    logs = sub.add_parser("logs", help="tail remote task logs")
    _add_connection_args(logs)
    logs.add_argument("task_id")
    logs.add_argument("--lines", type=int, default=50)

    tick = sub.add_parser("tick", help="run one scheduler tick from the control node")
    tick.add_argument("--config", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command_name == "daemon":
            run_daemon(args.config, args.log_level)
        elif args.command_name == "hash-password":
            _hash_password()
        elif args.command_name == "submit":
            _submit(args)
        elif args.command_name in ("tasks", "list"):
            _tasks(args)
        elif args.command_name == "show":
            _show(args)
        elif args.command_name == "events":
            _events(args)
        elif args.command_name == "wait":
            _wait(args)
        elif args.command_name == "gpus":
            _gpus(args)
        elif args.command_name == "workers":
            _workers(args)
        elif args.command_name == "status":
            _status(args)
        elif args.command_name == "start":
            _control_task(args, "start")
        elif args.command_name == "pause":
            _control_task(args, "pause")
        elif args.command_name == "rerun":
            _control_task(args, "rerun")
        elif args.command_name == "terminate":
            _control_task(args, "terminate")
        elif args.command_name == "cancel":
            _control_task(args, "terminate", label="canceled")
        elif args.command_name == "logs":
            _logs(args)
        elif args.command_name == "tick":
            _tick(args)
        else:
            parser.error("unknown command")
    except CliError as exc:
        raise SystemExit(str(exc)) from exc
    except KeyboardInterrupt as exc:
        raise SystemExit(130) from exc


def _add_connection_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--api-url", help="daemon API URL, for example http://127.0.0.1:8765")
    parser.add_argument("--config", help="control-node config file; used to resolve db_path and SSH settings")
    parser.add_argument("--db", help="SQLite DB path for direct local mode")


def _hash_password() -> None:
    password = getpass.getpass("Password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise CliError("passwords do not match")
    try:
        print(hash_password(password))
    except ValueError as exc:
        raise CliError(str(exc)) from exc


def _submit(args: argparse.Namespace) -> None:
    payload = _task_payload(args)
    if args.wait and payload["paused"]:
        raise CliError("--wait cannot be used with --paused")
    ctx = _context(args)
    if ctx.api_url:
        response = _api_post(ctx.api_url, "/tasks", payload)
        task = response["task"]
        if args.wait:
            task = _wait_for_task(
                ctx,
                str(task["id"]),
                interval_seconds=args.wait_interval,
                timeout_seconds=args.wait_timeout,
            )
        print(json.dumps(task, indent=2, sort_keys=True))
        if args.wait:
            _exit_for_terminal_task(task)
        return

    store = _store(ctx)
    if ctx.config_path and payload.get("target_worker"):
        workers = {worker.name for worker in load_config(ctx.config_path).workers}
        if payload["target_worker"] not in workers:
            raise CliError(f"unknown worker: {payload['target_worker']}")
    task = store.create_task(
        name=payload["name"],
        command=payload["command"],
        priority=payload["priority"],
        requested_gpus=payload["requested_gpus"],
        cwd=payload.get("cwd"),
        env=payload["env"],
        max_memory_used_mb=payload.get("max_memory_used_mb"),
        min_free_memory_mb=payload.get("min_free_memory_mb"),
        max_utilization_percent=payload.get("max_utilization_percent"),
        min_free_seconds=payload["min_free_seconds"],
        preemptible=payload["preemptible"],
        allow_preempt=payload["allow_preempt"],
        background=payload["background"],
        elastic=payload["elastic"],
        log_mode=payload["log_mode"],
        im_notify=payload["im_notify"],
        callback_command=payload.get("callback_command"),
        callback_events=payload.get("callback_events"),
        start_cron=payload.get("start_cron"),
        stop_cron=payload.get("stop_cron"),
        schedule_timezone=payload["schedule_timezone"],
        paused=payload["paused"],
        target_worker=payload.get("target_worker"),
    )
    task_data = task.as_dict()
    if args.wait:
        task_data = _wait_for_task(
            ctx,
            task.id,
            interval_seconds=args.wait_interval,
            timeout_seconds=args.wait_timeout,
        )
    print(json.dumps(task_data, indent=2, sort_keys=True))
    if args.wait:
        _exit_for_terminal_task(task_data)


def _tasks(args: argparse.Namespace) -> None:
    ctx = _context(args)
    if ctx.api_url:
        params = {"limit": max(1, args.limit), "offset": 0}
        if args.status:
            params["status"] = args.status
        tasks = _api_get(ctx.api_url, f"/tasks?{urlencode(params)}")["tasks"]
    else:
        tasks = [task.as_dict() for task in _store(ctx).list_tasks(status=args.status, limit=args.limit)]

    if args.json:
        print(json.dumps(tasks, indent=2, sort_keys=True))
        return
    _print_tasks(tasks)


def _show(args: argparse.Namespace) -> None:
    ctx = _context(args)
    if ctx.api_url:
        task = _api_get(ctx.api_url, f"/tasks/{args.task_id}")["task"]
    else:
        found = _store(ctx).get_task(args.task_id)
        if not found:
            raise CliError(f"task not found: {args.task_id}")
        task = found.as_dict()

    if args.json:
        print(json.dumps(task, indent=2, sort_keys=True))
        return
    rows = [[key, _display_value(value)] for key, value in task.items()]
    _print_table(["FIELD", "VALUE"], rows)


def _events(args: argparse.Namespace) -> None:
    ctx = _context(args)
    if ctx.api_url:
        events = _api_get(ctx.api_url, f"/tasks/{args.task_id}/events")["events"]
    else:
        events = _store(ctx).task_events(args.task_id)

    if args.json:
        print(json.dumps(events, indent=2, sort_keys=True))
        return
    rows = [[event["created_at"], event["event"], event.get("detail") or "-"] for event in events]
    _print_table(["CREATED_AT", "EVENT", "DETAIL"], rows)


def _wait(args: argparse.Namespace) -> None:
    ctx = _context(args)
    task = _wait_for_task(
        ctx,
        args.task_id,
        interval_seconds=args.interval,
        timeout_seconds=args.timeout,
    )
    if args.json:
        print(json.dumps(task, indent=2, sort_keys=True))
    else:
        rows = [[key, _display_value(value)] for key, value in task.items()]
        _print_table(["FIELD", "VALUE"], rows)
    _exit_for_terminal_task(task)


def _gpus(args: argparse.Namespace) -> None:
    ctx = _context(args)
    if ctx.api_url:
        states = _api_get(ctx.api_url, "/gpus")["gpus"]
        if args.worker:
            states = [state for state in states if state["worker_name"] == args.worker]
    else:
        states = _store(ctx).list_gpu_condition_states(args.worker)

    if args.json:
        print(json.dumps(states, indent=2, sort_keys=True))
        return
    rows = [
        [
            str(state["worker_name"]),
            str(state["gpu_index"]),
            str(state["condition_key"]),
            "yes" if state["is_free"] else "-",
            str(state["free_since"] or "-"),
            str(state["memory_used_mb"]),
            str(state["utilization_percent"]),
            str(state.get("numa_node") if state.get("numa_node") is not None else "-"),
            ",".join(str(user) for user in state.get("process_users", [])) or "-",
            str(state["last_seen"]),
        ]
        for state in states
    ]
    _print_table(
        ["WORKER", "GPU", "CONDITION", "FREE", "FREE_SINCE", "MEM_MB", "UTIL", "NUMA", "USERS", "LAST_SEEN"],
        rows,
    )


def _workers(args: argparse.Namespace) -> None:
    ctx = _context(args, require_config_for_local=True)
    if ctx.api_url:
        workers = _api_get(ctx.api_url, "/workers")["workers"]
    else:
        workers = [_worker_dict(worker) for worker in load_config(ctx.config_path).workers]

    if args.json:
        print(json.dumps(workers, indent=2, sort_keys=True))
        return
    rows = [
        [
            worker["name"],
            worker["host"],
            worker.get("user") or "-",
            str(worker["port"]),
            "yes" if worker["enabled"] else "-",
            str(worker.get("connection_state") or ("unknown" if worker["enabled"] else "disabled")),
            ",".join(map(str, worker["gpus"])) or "auto",
            str(worker.get("max_concurrent_tasks") or "unlimited"),
            str(worker.get("max_background_tasks", 1)),
        ]
        for worker in workers
    ]
    _print_table(["NAME", "HOST", "USER", "PORT", "ENABLED", "CONNECTION", "GPUS", "MAX_TASKS", "MAX_BG"], rows)


def _status(args: argparse.Namespace) -> None:
    ctx = _context(args)
    api_summary: dict[str, Any] | None = None
    if ctx.api_url:
        tasks_response = _api_get(ctx.api_url, "/tasks?limit=1&offset=0")
        tasks = tasks_response["tasks"]
        api_summary = tasks_response.get("summary")
        workers = _api_get(ctx.api_url, "/workers")["workers"]
        gpus = _api_get(ctx.api_url, "/gpus")["gpus"]
    else:
        store = _store(ctx)
        tasks = [task.as_dict() for task in store.list_tasks(limit=1000)]
        gpus = store.list_gpu_condition_states()
        workers = [_worker_dict(worker) for worker in load_config(ctx.config_path).workers] if ctx.config_path else []

    summary = {
        "queued": _summary_count(api_summary, tasks, "queued"),
        "paused": _summary_count(api_summary, tasks, "paused"),
        "running": _summary_count(api_summary, tasks, "running"),
        "succeeded": _summary_count(api_summary, tasks, "succeeded"),
        "failed": _summary_count(api_summary, tasks, "failed"),
        "canceled": _summary_count(api_summary, tasks, "canceled"),
        "workers": len(workers),
        "observed_gpus": len({(gpu["worker_name"], gpu["gpu_index"]) for gpu in gpus}),
    }
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
        return
    _print_table(["METRIC", "VALUE"], [[key, str(value)] for key, value in summary.items()])


def _control_task(args: argparse.Namespace, action: str, *, label: str | None = None) -> None:
    ctx = _context(args)
    label = label or action
    if ctx.api_url:
        response = _api_post(ctx.api_url, f"/tasks/{args.task_id}/{action}", {})
        if not response.get("ok"):
            raise CliError(f"task cannot be {label}: {args.task_id}")
        print(f"{label} {args.task_id}")
        return

    store = _store(ctx)
    if ctx.config_path:
        config = load_config(ctx.config_path)
        scheduler = Scheduler(config, store, RemoteExecutor(config.daemon.remote_base_dir))
        changed = _scheduler_control(scheduler, action, args.task_id)
    else:
        changed = _store_control(store, action, args.task_id)
    if not changed:
        raise CliError(f"task cannot be {label}: {args.task_id}")
    print(f"{label} {args.task_id}")


def _summary_count(api_summary: dict[str, Any] | None, tasks: list[dict[str, Any]], status: str) -> int:
    if api_summary is not None and status in api_summary:
        return int(api_summary[status])
    return sum(1 for task in tasks if task["status"] == status)


def _logs(args: argparse.Namespace) -> None:
    ctx = _context(args)
    if ctx.api_url:
        response = _api_get(ctx.api_url, f"/tasks/{args.task_id}/logs?lines={max(1, args.lines)}")
        logs = response.get("logs") or ""
        if logs:
            print(logs)
        return
    if not ctx.config_path:
        raise CliError("--config is required for local log tailing unless --api-url is used")

    config = load_config(ctx.config_path)
    store = _store(ctx)
    task = store.get_task(args.task_id)
    if task is None:
        raise CliError(f"task not found: {args.task_id}")
    if not task.assigned_worker:
        raise CliError(f"task has no assigned worker: {args.task_id}")
    worker = {worker.name: worker for worker in config.workers}.get(task.assigned_worker)
    if worker is None:
        raise CliError(f"worker not in config: {task.assigned_worker}")
    print(RemoteExecutor(config.daemon.remote_base_dir).tail_logs(worker, task.id, lines=args.lines))


def _tick(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    store = Store(config.daemon.db_path)
    store.init()
    store.sync_workers(config.workers)
    Scheduler(config, store).tick()
    print("tick complete")


def _task_payload(args: argparse.Namespace) -> dict[str, Any]:
    command_parts = list(args.task_command)
    if command_parts and command_parts[0] == "--":
        command_parts = command_parts[1:]
    command = " ".join(command_parts).strip()
    if not command:
        raise CliError("task command is required")

    payload: dict[str, Any] = {
        "name": args.name,
        "command": command,
        "priority": args.priority,
        "requested_gpus": args.gpus,
        "env": _parse_env(args.env),
        "target_worker": _normalize_target_worker(args.target_worker),
        "min_free_seconds": args.min_free_seconds,
        "preemptible": args.preemptible,
        "allow_preempt": args.allow_preempt,
        "background": args.background,
        "elastic": args.elastic,
        "log_mode": args.log_mode,
        "im_notify": args.im_notify,
        "callback_command": args.callback_command,
        "callback_events": args.callback_on,
        "start_cron": args.start_cron,
        "stop_cron": args.stop_cron,
        "schedule_timezone": args.schedule_timezone,
        "paused": args.paused,
    }
    if args.cwd:
        payload["cwd"] = args.cwd
    if args.max_memory_used_mb is not None:
        payload["max_memory_used_mb"] = args.max_memory_used_mb
    if args.min_free_memory_mb is not None:
        payload["min_free_memory_mb"] = args.min_free_memory_mb
    if args.max_utilization_percent is not None:
        payload["max_utilization_percent"] = args.max_utilization_percent
    return payload


def _scheduler_control(scheduler: Scheduler, action: str, task_id: str) -> bool:
    if action == "start":
        return scheduler.start_task(task_id)
    if action == "pause":
        return scheduler.pause_task(task_id)
    if action == "rerun":
        return scheduler.rerun_task(task_id)
    if action == "terminate":
        return scheduler.cancel_task(task_id)
    raise CliError(f"unknown task control action: {action}")


def _store_control(store: Store, action: str, task_id: str) -> bool:
    if action == "start":
        return store.start_task(task_id)
    if action == "pause":
        return store.pause_task(task_id)
    if action == "rerun":
        return store.rerun_task(task_id)
    if action == "terminate":
        return store.cancel_task(task_id)
    raise CliError(f"unknown task control action: {action}")


def _context(args: argparse.Namespace, *, require_config_for_local: bool = False) -> CliContext:
    config_path = getattr(args, "config", None)
    db_path = getattr(args, "db", None)
    api_url = getattr(args, "api_url", None)
    if api_url:
        return CliContext(db_path=db_path, config_path=config_path, api_url=api_url.rstrip("/"))
    if config_path and not db_path:
        db_path = load_config(config_path).daemon.db_path
    if not db_path and not config_path:
        api_url = os.environ.get(API_URL_ENV, DEFAULT_API_URL)
        return CliContext(db_path=None, config_path=None, api_url=api_url.rstrip("/"))
    if require_config_for_local and not config_path:
        raise CliError("--config is required for this command unless --api-url is used")
    if not db_path:
        raise CliError("provide --api-url, --config, or --db")
    return CliContext(db_path=db_path, config_path=config_path, api_url=None)


def _store(ctx: CliContext) -> Store:
    if not ctx.db_path:
        raise CliError("db path is not available")
    store = Store(ctx.db_path)
    store.init()
    return store


def _api_get(api_url: str, path: str) -> dict[str, Any]:
    return _api_request(api_url, path, method="GET")


def _api_post(api_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _api_request(api_url, path, method="POST", payload=payload)


def _wait_for_task(
    ctx: CliContext,
    task_id: str,
    *,
    interval_seconds: float,
    timeout_seconds: float | None,
) -> dict[str, Any]:
    if interval_seconds <= 0:
        raise CliError("wait interval must be positive")
    if timeout_seconds is not None and timeout_seconds < 0:
        raise CliError("wait timeout cannot be negative")

    started = time.monotonic()
    while True:
        task = _fetch_task(ctx, task_id)
        if str(task.get("status")) in TERMINAL_TASK_STATUSES:
            return task
        elapsed = time.monotonic() - started
        if timeout_seconds is not None and elapsed >= timeout_seconds:
            raise CliError(f"timed out waiting for task {task_id}; last status={task.get('status')}")
        sleep_seconds = interval_seconds
        if timeout_seconds is not None:
            sleep_seconds = min(sleep_seconds, max(0.0, timeout_seconds - elapsed))
        time.sleep(sleep_seconds)


def _fetch_task(ctx: CliContext, task_id: str) -> dict[str, Any]:
    if ctx.api_url:
        return _api_get(ctx.api_url, f"/tasks/{task_id}")["task"]
    task = _store(ctx).get_task(task_id)
    if task is None:
        raise CliError(f"task not found: {task_id}")
    return task.as_dict()


def _exit_for_terminal_task(task: dict[str, Any]) -> None:
    status = str(task.get("status") or "")
    if status == "succeeded":
        return
    if status == "failed":
        raise SystemExit(_process_exit_code(task.get("exit_code"), fallback=1))
    if status == "canceled":
        raise SystemExit(130)
    raise SystemExit(1)


def _process_exit_code(value: Any, *, fallback: int) -> int:
    try:
        code = int(value)
    except (TypeError, ValueError):
        return fallback
    if code <= 0:
        return fallback
    return min(code, 255)


def _api_request(
    api_url: str,
    path: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = urljoin(f"{api_url.rstrip('/')}/", path.lstrip("/"))
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    request = Request(
        url,
        data=body,
        method=method,
        headers=_api_headers(),
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8")
        try:
            payload = json.loads(detail)
            message = payload.get("error", detail)
        except json.JSONDecodeError:
            message = detail or exc.reason
        raise CliError(f"API {exc.code}: {message}") from exc
    except URLError as exc:
        raise CliError(f"API request failed: {exc.reason}") from exc


def _api_headers() -> dict[str, str]:
    return {
        "accept": "application/json",
        "content-type": "application/json",
        LOCAL_CLIENT_HEADER: "1",
    }


def _parse_env(items: list[str]) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise CliError(f"--env must be KEY=VALUE: {item}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise CliError(f"--env key is empty: {item}")
        env[key] = value
    return env


def _print_tasks(tasks: list[dict[str, Any]]) -> None:
    rows = [
        [
            str(task["id"])[:8],
            str(task["status"]),
            str(task["priority"]),
            str(task["requested_gpus"]),
            str(task.get("assigned_worker") or "-"),
            str(task.get("target_worker") or "auto"),
            ",".join(map(str, task.get("assigned_gpus") or [])) or "-",
            _format_duration(task.get("duration_seconds")),
            "yes" if task.get("allow_preempt") else "-",
            "yes" if task.get("preemptible") else "-",
            "yes" if task.get("background") else "-",
            "yes" if task.get("elastic") else "-",
            "yes" if task.get("im_notify") else "-",
            str(task.get("log_mode") or "pty"),
            str(task["name"]),
        ]
        for task in tasks
    ]
    _print_table(["ID", "STATUS", "PRI", "GPU", "WORKER", "TARGET", "DEVICES", "DURATION", "PREEMPT", "YIELD", "BG", "ELASTIC", "NOTIFY", "LOG", "NAME"], rows)


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    fmt = "  ".join(f"{{:<{width}}}" for width in widths)
    print(fmt.format(*headers))
    print(fmt.format(*["-" * width for width in widths]))
    for row in rows:
        print(fmt.format(*row))


def _display_value(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _format_duration(value: Any) -> str:
    if value is None:
        return "-"
    seconds = int(value)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{seconds:02d}s"
    if minutes:
        return f"{minutes}m{seconds:02d}s"
    return f"{seconds}s"


def _normalize_target_worker(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text or text.lower() == "auto":
        return None
    return text


def _worker_dict(worker) -> dict[str, Any]:
    return {
        "name": worker.name,
        "host": worker.host,
        "user": worker.user,
        "port": worker.port,
        "ssh_key": worker.ssh_key,
        "enabled": worker.enabled,
        "tmux_prefix": worker.tmux_prefix,
        "gpus": worker.gpus,
        "max_concurrent_tasks": worker.max_concurrent_tasks,
        "max_background_tasks": worker.max_background_tasks,
    }


class CliError(RuntimeError):
    pass


if __name__ == "__main__":
    main(sys.argv[1:])
