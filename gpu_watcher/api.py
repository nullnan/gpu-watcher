from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
from importlib import resources
from typing import Any
from urllib.parse import urlsplit

from aiohttp import web

from .auth import AuthManager, AuthSession
from .config import ConfigError, parse_worker_config
from .models import AppConfig
from .scheduler import Scheduler
from .store import Store

LOGGER = logging.getLogger(__name__)
AUTH_TYPE_KEY = web.RequestKey("auth_type", str)
AUTH_SESSION_KEY = web.RequestKey("auth_session", AuthSession)
LOCAL_CLI_HEADER = "x-gpu-watcher-local-client"


def serve_api(config: AppConfig, store: Store, scheduler: Scheduler) -> None:
    app = _make_app(config, store, scheduler)
    LOGGER.info("HTTP API listening on http://%s:%s", config.api.host, config.api.port)
    web.run_app(
        app,
        host=config.api.host,
        port=config.api.port,
        print=None,
        access_log=None,
    )


def _make_app(config: AppConfig, store: Store, scheduler: Scheduler) -> web.Application:
    auth = AuthManager(config.auth)

    @web.middleware
    async def security_headers(request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            response = await handler(request)
        except web.HTTPException as exc:
            _apply_security_headers(request, exc)
            raise
        _apply_security_headers(request, response)
        return response

    @web.middleware
    async def authenticate(request: web.Request, handler: Any) -> web.StreamResponse:
        if not config.auth.enabled:
            request[AUTH_TYPE_KEY] = "disabled"
            return await handler(request)

        if _is_local_cli_request(request):
            request[AUTH_TYPE_KEY] = "local_cli"
            return await handler(request)

        if _is_public_path(request.path):
            return await handler(request)

        session = auth.verify_session(request.cookies.get(config.auth.cookie_name))
        if session is None:
            if request.path in {"/", "/index.html"}:
                raise web.HTTPFound("/login")
            return _json_error("authentication required", 401)

        request[AUTH_TYPE_KEY] = "session"
        request[AUTH_SESSION_KEY] = session
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            if not _valid_same_origin(request):
                return _json_error("same-origin request required", 403)
            if not _valid_csrf(request, session):
                return _json_error("invalid CSRF token", 403)
        if request.path.endswith("/stream") and not _valid_same_origin(request):
            return _json_error("same-origin WebSocket required", 403)
        return await handler(request)

    app = web.Application(middlewares=[security_headers, authenticate])

    async def index(request: web.Request) -> web.StreamResponse:
        return _static_response("index.html", "text/html; charset=utf-8")

    async def login_page(request: web.Request) -> web.StreamResponse:
        if config.auth.enabled and auth.verify_session(request.cookies.get(config.auth.cookie_name)):
            raise web.HTTPFound("/")
        return _static_response("login.html", "text/html; charset=utf-8")

    async def favicon(request: web.Request) -> web.StreamResponse:
        return _static_response("favicon.ico", "image/x-icon")

    async def static_asset(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        content_types = {
            "styles.css": "text/css; charset=utf-8",
            "app.js": "text/javascript; charset=utf-8",
            "login.js": "text/javascript; charset=utf-8",
            "theme.js": "text/javascript; charset=utf-8",
        }
        if name not in content_types:
            raise web.HTTPNotFound(text="not found")
        return _static_response(name, content_types[name])

    async def vendor_asset(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        if "/" in name or name.startswith("."):
            raise web.HTTPNotFound(text="not found")
        if name.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif name.endswith(".js"):
            content_type = "text/javascript; charset=utf-8"
        elif name.endswith(".woff2"):
            content_type = "font/woff2"
        else:
            raise web.HTTPNotFound(text="not found")
        return _static_response(f"vendor/{name}", content_type)

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def login(request: web.Request) -> web.Response:
        if not config.auth.enabled:
            return web.json_response({"ok": True, "auth_enabled": False})
        if not _valid_same_origin(request):
            return _json_error("same-origin request required", 403)
        client_key = _client_key(request)
        allowed, retry_after = auth.login_allowed(client_key)
        if not allowed:
            return web.json_response(
                {"error": "too many login attempts"},
                status=429,
                headers={"retry-after": str(retry_after)},
            )
        try:
            payload = await request.json()
        except Exception:
            return _json_error("invalid json", 400)
        username = str(payload.get("username", ""))[:256]
        password = str(payload.get("password", ""))[:4096]
        if not auth.verify_credentials(username, password):
            auth.record_login_failure(client_key)
            return _json_error("invalid username or password", 401)
        auth.clear_login_failures(client_key)
        token, session = auth.create_session()
        response = web.json_response(
            {
                "ok": True,
                "username": session.username,
                "expires_at": session.expires_at,
                "csrf_token": session.csrf_token,
            }
        )
        response.set_cookie(
            config.auth.cookie_name,
            token,
            httponly=True,
            secure=config.auth.secure_cookie,
            samesite="Strict",
            max_age=config.auth.session_ttl_seconds,
            path="/",
        )
        return response

    async def auth_session(request: web.Request) -> web.Response:
        session = request.get(AUTH_SESSION_KEY)
        if not config.auth.enabled:
            return web.json_response({"authenticated": True, "auth_enabled": False})
        if request.get(AUTH_TYPE_KEY) == "local_cli":
            return web.json_response(
                {"authenticated": True, "auth_enabled": True, "auth_type": "local_cli"}
            )
        assert isinstance(session, AuthSession)
        return web.json_response(
            {
                "authenticated": True,
                "auth_enabled": True,
                "username": session.username,
                "expires_at": session.expires_at,
                "csrf_token": session.csrf_token,
            }
        )

    async def logout(request: web.Request) -> web.Response:
        response = web.json_response({"ok": True})
        response.del_cookie(
            config.auth.cookie_name,
            path="/",
        )
        return response

    async def workers(request: web.Request) -> web.Response:
        healths = store.worker_healths()
        return web.json_response(
            {"workers": [_worker_dict(worker, healths.get(worker.name)) for worker in config.workers]}
        )

    async def update_worker(request: web.Request) -> web.Response:
        worker_name = request.match_info["worker_name"]
        existing = scheduler.workers.get(worker_name)
        if existing is None:
            return _json_error("worker not found", 404)
        try:
            payload = await request.json()
        except Exception as exc:
            return _json_error(f"invalid json: {exc}", 400)
        if not isinstance(payload, dict):
            return _json_error("worker payload must be an object", 400)

        allowed_fields = {
            "name",
            "host",
            "user",
            "port",
            "ssh_key",
            "enabled",
            "tmux_prefix",
            "gpus",
            "max_concurrent_tasks",
            "max_background_tasks",
        }
        unknown_fields = set(payload) - allowed_fields
        if unknown_fields:
            return _json_error(f"unknown worker fields: {', '.join(sorted(unknown_fields))}", 400)
        if "gpus" in payload and not isinstance(payload["gpus"], list):
            return _json_error("worker gpus must be an array", 400)

        options = _worker_dict(existing)
        options.update(payload)
        try:
            worker = parse_worker_config(options)
            updated = await asyncio.to_thread(scheduler.update_worker, worker_name, worker)
        except ConfigError as exc:
            return _json_error(str(exc), 400)
        except OSError as exc:
            LOGGER.exception("failed to persist worker %s", worker_name)
            return _json_error(f"failed to persist worker config: {exc}", 500)
        return web.json_response({"worker": _worker_dict(updated)})

    async def gpus(request: web.Request) -> web.Response:
        return web.json_response({"gpus": store.list_gpu_condition_states()})

    async def list_tasks(request: web.Request) -> web.Response:
        status = _query_text(request, "status")
        search = _query_text(request, "q")
        limit = _bounded_int(_query_int(request, "limit", 25), 1, 200)
        offset = max(0, _query_int(request, "offset", 0))
        tasks = store.list_tasks(status=status, limit=limit, offset=offset, search=search)
        return web.json_response(
            {
                "tasks": [task.as_dict() for task in tasks],
                "total": store.count_tasks(status=status, search=search),
                "limit": limit,
                "offset": offset,
                "summary": store.task_summary(),
            }
        )

    async def get_task(request: web.Request) -> web.Response:
        task = store.get_task(request.match_info["task_id"])
        if task is None:
            return _json_error("task not found", 404)
        return web.json_response({"task": task.as_dict()})

    async def task_events(request: web.Request) -> web.Response:
        return web.json_response({"events": store.task_events(request.match_info["task_id"])})

    async def task_logs(request: web.Request) -> web.Response:
        task_id = request.match_info["task_id"]
        lines = _query_int(request, "lines", 50)
        task = store.get_task(task_id)
        if task is None:
            return _json_error("task not found", 404)
        if not task.assigned_worker:
            return _json_error("task has no assigned worker", 409)
        worker = scheduler.workers.get(task.assigned_worker)
        if worker is None:
            return _json_error(f"worker not in daemon config: {task.assigned_worker}", 409)
        try:
            logs = await asyncio.to_thread(scheduler.remote.tail_logs, worker, task.id, max(1, lines))
        except Exception as exc:
            return _json_error(str(exc), 502)
        return web.json_response(
            {
                "task_id": task.id,
                "assigned_worker": task.assigned_worker,
                "assigned_gpus": task.assigned_gpus,
                "lines": max(1, lines),
                "logs": logs,
            }
        )

    async def task_log_stream(request: web.Request) -> web.StreamResponse:
        task_id = request.match_info["task_id"]
        tail_bytes = _bounded_int(_query_int(request, "tail_bytes", 262144), 0, 8 * 1024 * 1024)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        task = store.get_task(task_id)
        if task is None:
            await _send_ws_error(ws, "task not found")
            return ws
        if not task.assigned_worker:
            await _send_ws_error(ws, "task has no assigned worker")
            return ws
        worker = scheduler.workers.get(task.assigned_worker)
        if worker is None:
            await _send_ws_error(ws, f"worker not in daemon config: {task.assigned_worker}")
            return ws

        first_connection = True
        while not ws.closed:
            proc: asyncio.subprocess.Process | None = None
            closed: asyncio.Task[None] | None = None
            streamed: asyncio.Task[None] | None = None
            try:
                proc = await scheduler.remote.open_log_stream(
                    worker,
                    task.id,
                    tail_bytes=tail_bytes if first_connection else 0,
                )
                closed = asyncio.create_task(_wait_for_ws_close(ws))
                streamed = asyncio.create_task(_stream_process_to_ws(proc, ws))
                done, pending = await asyncio.wait(
                    {closed, streamed},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                websocket_closed = closed in done
                for task_done in done:
                    with contextlib.suppress(Exception):
                        task_done.result()
                for task_pending in pending:
                    task_pending.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task_pending
                if websocket_closed:
                    break
            except Exception as exc:
                if first_connection:
                    await _send_ws_error(ws, str(exc))
                    return ws
            finally:
                if closed is not None and not closed.done():
                    closed.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await closed
                if streamed is not None and not streamed.done():
                    streamed.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await streamed
                if proc is not None:
                    await _terminate_process(proc)
            if ws.closed:
                break
            first_connection = False
            await asyncio.sleep(1)
        with contextlib.suppress(Exception):
            await ws.close()
        return ws

    async def worker_nvtop_stream(request: web.Request) -> web.StreamResponse:
        worker_name = request.match_info["worker_name"]
        cols = _bounded_int(_query_int(request, "cols", 120), 20, 400)
        rows = _bounded_int(_query_int(request, "rows", 36), 8, 200)
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        worker = scheduler.workers.get(worker_name)
        if worker is None:
            await _send_ws_error(ws, f"worker not in daemon config: {worker_name}")
            return ws

        session = None
        streamed: asyncio.Task[None] | None = None
        controlled: asyncio.Task[None] | None = None
        try:
            session = await scheduler.remote.open_nvtop(worker, cols=cols, rows=rows)
            streamed = asyncio.create_task(_stream_pty_to_ws(session, ws))
            controlled = asyncio.create_task(_relay_ws_to_pty(ws, session))
            done, pending = await asyncio.wait(
                {streamed, controlled},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for completed in done:
                with contextlib.suppress(Exception):
                    completed.result()
            for remaining in pending:
                remaining.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await remaining
        except Exception as exc:
            if not ws.closed:
                await ws.send_str(f"gpu-watcher: {exc}\r\n")
        finally:
            for relay in (streamed, controlled):
                if relay is not None and not relay.done():
                    relay.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await relay
            if session is not None:
                await session.terminate()
            with contextlib.suppress(Exception):
                await ws.close()
        return ws

    async def create_task(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception as exc:
            return _json_error(f"invalid json: {exc}", 400)
        try:
            task = store.create_task(
                **_task_options_from_payload(payload, config),
                paused=bool(payload.get("paused", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            return _json_error(str(exc), 400)
        return web.json_response({"task": task.as_dict()}, status=201)

    async def update_task(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception as exc:
            return _json_error(f"invalid json: {exc}", 400)
        try:
            task = await asyncio.to_thread(
                scheduler.update_task,
                request.match_info["task_id"],
                **_task_options_from_payload(payload, config),
            )
        except (KeyError, TypeError, ValueError) as exc:
            return _json_error(str(exc), 400)
        if task is None:
            existing = store.get_task(request.match_info["task_id"])
            if existing is None:
                return _json_error("task not found", 404)
            return _json_error("only paused or unstarted queued tasks can be edited", 409)
        return web.json_response({"task": task.as_dict()})

    async def start_task(request: web.Request) -> web.Response:
        task_id = request.match_info["task_id"]
        previous_event_count = len(store.task_events(task_id))
        changed = await asyncio.to_thread(scheduler.start_task, task_id)
        if not changed:
            return _task_control_error(store, task_id, "start", previous_event_count)
        return web.json_response({"ok": True})

    async def pause_task(request: web.Request) -> web.Response:
        task_id = request.match_info["task_id"]
        previous_event_count = len(store.task_events(task_id))
        changed = await asyncio.to_thread(scheduler.pause_task, task_id)
        if not changed:
            return _task_control_error(store, task_id, "pause", previous_event_count)
        return web.json_response({"ok": True})

    async def rerun_task(request: web.Request) -> web.Response:
        task_id = request.match_info["task_id"]
        previous_event_count = len(store.task_events(task_id))
        changed = await asyncio.to_thread(scheduler.rerun_task, task_id)
        if not changed:
            return _task_control_error(store, task_id, "rerun", previous_event_count)
        return web.json_response({"ok": True})

    async def terminate_task(request: web.Request) -> web.Response:
        task_id = request.match_info["task_id"]
        previous_event_count = len(store.task_events(task_id))
        changed = await asyncio.to_thread(scheduler.cancel_task, task_id)
        if not changed:
            return _task_control_error(store, task_id, "terminate", previous_event_count)
        return web.json_response({"ok": True})

    app.router.add_get("/", index)
    app.router.add_get("/index.html", index)
    app.router.add_get("/login", login_page)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_get("/static/{name}", static_asset)
    app.router.add_get("/static/vendor/{name}", vendor_asset)
    app.router.add_get("/health", health)
    app.router.add_post("/auth/login", login)
    app.router.add_get("/auth/session", auth_session)
    app.router.add_post("/auth/logout", logout)
    app.router.add_get("/workers", workers)
    app.router.add_get("/workers/{worker_name}/nvtop/stream", worker_nvtop_stream)
    app.router.add_put("/workers/{worker_name}", update_worker)
    app.router.add_get("/gpus", gpus)
    app.router.add_get("/tasks", list_tasks)
    app.router.add_post("/tasks", create_task)
    app.router.add_get("/tasks/{task_id}", get_task)
    app.router.add_put("/tasks/{task_id}", update_task)
    app.router.add_get("/tasks/{task_id}/events", task_events)
    app.router.add_get("/tasks/{task_id}/logs", task_logs)
    app.router.add_get("/tasks/{task_id}/logs/stream", task_log_stream)
    app.router.add_post("/tasks/{task_id}/start", start_task)
    app.router.add_post("/tasks/{task_id}/pause", pause_task)
    app.router.add_post("/tasks/{task_id}/rerun", rerun_task)
    app.router.add_post("/tasks/{task_id}/terminate", terminate_task)
    app.router.add_post("/tasks/{task_id}/cancel", terminate_task)
    return app


def _is_public_path(path: str) -> bool:
    return path in {"/login", "/auth/login", "/health", "/favicon.ico"} or path.startswith("/static/")


def _is_local_cli_request(request: web.Request) -> bool:
    return (
        request.remote in {"127.0.0.1", "::1"}
        and "x-forwarded-for" not in request.headers
        and request.headers.get(LOCAL_CLI_HEADER) == "1"
    )


def _task_control_error(
    store: Store,
    task_id: str,
    action: str,
    previous_event_count: int,
) -> web.Response:
    task = store.get_task(task_id)
    if task is None:
        return _json_error("task not found", 404)

    failure_events = {
        "pause": {"pause_kill_failed", "pause_rejected"},
        "terminate": {"cancel_kill_failed"},
    }.get(action, set())
    for event in reversed(store.task_events(task_id)[previous_event_count:]):
        if event["event"] in failure_events and event.get("detail"):
            return _json_error(f"{action} failed: {event['detail']}", 409)

    expected_status = {
        "start": "paused or queued",
        "pause": "preemptible and queued or running",
        "rerun": "failed",
        "terminate": "paused, queued, or running",
    }[action]
    return _json_error(
        f"cannot {action} task in {task.status} state; expected {expected_status}",
        409,
    )


def _apply_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.update(
        {
            "content-security-policy": (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self' ws: wss:; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            ),
            "x-content-type-options": "nosniff",
            "x-frame-options": "DENY",
            "referrer-policy": "no-referrer",
            "permissions-policy": "camera=(), microphone=(), geolocation=()",
        }
    )
    if request.path not in {"/favicon.ico"} and not request.path.startswith("/static/"):
        response.headers["cache-control"] = "no-store"


def _valid_csrf(request: web.Request, session: AuthSession) -> bool:
    supplied = request.headers.get("x-csrf-token", "")
    return bool(supplied and hmac.compare_digest(supplied, session.csrf_token))


def _valid_same_origin(request: web.Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return False
    parsed = urlsplit(origin)
    return parsed.scheme in {"http", "https"} and parsed.netloc == request.host


def _client_key(request: web.Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and request.remote in {"127.0.0.1", "::1"}:
        return forwarded.rsplit(",", 1)[-1].strip()
    return request.remote or "unknown"


async def _stream_process_to_ws(proc: asyncio.subprocess.Process, ws: web.WebSocketResponse) -> None:
    assert proc.stdout is not None
    while True:
        chunk = await proc.stdout.read(32768)
        if not chunk:
            break
        await ws.send_bytes(chunk)


async def _stream_pty_to_ws(session: Any, ws: web.WebSocketResponse) -> None:
    while not ws.closed:
        chunk = await session.read(32768)
        if not chunk:
            break
        await ws.send_bytes(chunk)


async def _relay_ws_to_pty(ws: web.WebSocketResponse, session: Any) -> None:
    async for message in ws:
        if message.type == web.WSMsgType.BINARY:
            await session.write(bytes(message.data[:65536]))
            continue
        if message.type != web.WSMsgType.TEXT:
            continue
        try:
            payload = json.loads(message.data)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        message_type = payload.get("type")
        if message_type == "input" and isinstance(payload.get("data"), str):
            await session.write(payload["data"].encode("utf-8")[:65536])
        elif message_type == "resize":
            try:
                cols = _bounded_int(int(payload.get("cols", 120)), 20, 400)
                rows = _bounded_int(int(payload.get("rows", 36)), 8, 200)
            except (TypeError, ValueError):
                continue
            session.resize(cols, rows)


async def _wait_for_ws_close(ws: web.WebSocketResponse) -> None:
    async for _ in ws:
        pass


async def _terminate_process(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


async def _send_ws_error(ws: web.WebSocketResponse, message: str) -> None:
    await ws.send_str(f"gpu-watcher: {message}\r\n")
    await ws.close()


def _static_response(name: str, content_type: str) -> web.Response:
    try:
        data = resources.files("gpu_watcher.web").joinpath(name).read_bytes()
    except FileNotFoundError as exc:
        raise web.HTTPNotFound(text="not found") from exc
    mime_type, _, charset_part = content_type.partition(";")
    charset = charset_part.replace("charset=", "").strip() or None
    return web.Response(
        body=data,
        content_type=mime_type.strip(),
        charset=charset,
        headers={"cache-control": "no-cache"},
    )


def _json_error(message: str, status: int) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_str_list(value: Any) -> list[str] | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        return [value]
    return [str(item) for item in value]


def _query_int(request: web.Request, key: str, default: int) -> int:
    value = request.query.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _query_text(request: web.Request, key: str) -> str | None:
    value = request.query.get(key)
    if value is None:
        return None
    text = value.strip()
    return text or None


def _bounded_int(value: int, minimum: int, maximum: int) -> int:
    return min(maximum, max(minimum, value))


def _normalize_target_worker(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "auto":
        return None
    return text


def _task_options_from_payload(payload: Any, config: AppConfig) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TypeError("json payload must be an object")
    target_worker = _normalize_target_worker(payload.get("target_worker"))
    if target_worker and target_worker not in {worker.name for worker in config.workers}:
        raise ValueError(f"unknown worker: {target_worker}")
    return {
        "name": str(payload.get("name") or "task"),
        "command": str(payload["command"]),
        "priority": int(payload.get("priority", 0)),
        "requested_gpus": int(payload.get("requested_gpus", 1)),
        "env": {str(k): str(v) for k, v in dict(payload.get("env", {})).items()},
        "cwd": _optional_text(payload.get("cwd")),
        "target_worker": target_worker,
        "max_memory_used_mb": _optional_int(payload.get("max_memory_used_mb")),
        "min_free_memory_mb": _optional_int(payload.get("min_free_memory_mb")),
        "max_utilization_percent": _optional_int(payload.get("max_utilization_percent")),
        "min_free_seconds": int(payload.get("min_free_seconds", 0)),
        "preemptible": bool(payload.get("preemptible", False)),
        "allow_preempt": bool(payload.get("allow_preempt", False)),
        "background": bool(payload.get("background", False)),
        "elastic": bool(payload.get("elastic", False)),
        "log_mode": str(payload.get("log_mode") or "pty"),
        "im_notify": bool(payload.get("im_notify", False)),
        "callback_command": _optional_text(payload.get("callback_command")),
        "callback_events": _optional_str_list(payload.get("callback_events")),
        "start_cron": _optional_text(payload.get("start_cron")),
        "stop_cron": _optional_text(payload.get("stop_cron")),
        "schedule_timezone": str(payload.get("schedule_timezone") or "UTC"),
    }


def _worker_dict(worker, health: dict[str, str | None] | None = None) -> dict[str, Any]:
    payload = {
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
    if health is not None:
        payload.update(health)
        if not worker.enabled:
            payload["connection_state"] = "disabled"
    return payload
