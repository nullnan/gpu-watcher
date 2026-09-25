from __future__ import annotations

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from gpu_watcher.api import LOCAL_CLI_HEADER, _make_app
from gpu_watcher.auth import hash_password
from gpu_watcher.models import AuthConfig, ApiConfig, AppConfig, DaemonConfig, WorkerConfig
from gpu_watcher.scheduler import Scheduler
from gpu_watcher.store import Store


class ApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        worker = WorkerConfig(name="node-a", host="node-a", ssh_key="~/.ssh/id_ed25519", gpus=[0, 1])
        self.config_path = Path(self.tmp.name) / "config.toml"
        self.config_path.write_text(
            """
[[workers]]
name = "node-a"
host = "node-a"
ssh_key = "~/.ssh/id_ed25519"
gpus = [0, 1]
""",
            encoding="utf-8",
        )
        self.config = AppConfig(
            daemon=DaemonConfig(db_path=str(Path(self.tmp.name) / "test.db")),
            api=ApiConfig(enabled=True),
            workers=[worker],
            config_path=str(self.config_path),
        )
        self.store = Store(self.config.daemon.db_path)
        self.store.init()
        self.store.sync_workers(self.config.workers)
        scheduler = Scheduler(self.config, self.store)
        self.scheduler = scheduler
        self.client = TestClient(TestServer(_make_app(self.config, self.store, scheduler)))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tmp.cleanup()

    async def test_frontend_fonts_and_bundles_are_served(self) -> None:
        for path, content_type in [
            ("/static/vendor/inter-latin-wght-normal.woff2", "font/woff2"),
            ("/static/vendor/ui-style.js", "text/javascript"),
            ("/static/app.js", "text/javascript"),
            ("/static/styles.css", "text/css"),
        ]:
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertEqual(response.content_type, content_type)
            self.assertTrue(await response.read())
        response = await self.client.get("/static/vendor/missing.woff2")
        self.assertEqual(response.status, 404)

    async def test_health_remains_responsive_while_scheduler_update_blocks(self) -> None:
        task = self.store.create_task(name="blocked", command="echo old")
        original_update = self.scheduler.update_task

        def slow_update(task_id: str, **options: object):
            time.sleep(0.3)
            return original_update(task_id, **options)

        self.scheduler.update_task = Mock(side_effect=slow_update)
        update_request = asyncio.create_task(
            self.client.put(
                f"/tasks/{task.id}",
                json={"name": "updated", "command": "echo updated"},
            )
        )
        health_request = asyncio.create_task(self.client.get("/health"))

        done, pending = await asyncio.wait(
            {update_request, health_request},
            timeout=0.1,
        )

        self.assertIn(health_request, done)
        self.assertIn(update_request, pending)
        health_response = await health_request
        self.assertEqual(health_response.status, 200)
        update_response = await update_request
        self.assertEqual(update_response.status, 200)

    async def test_put_updates_unstarted_queued_task(self) -> None:
        task = self.store.create_task(name="old", command="echo old")

        response = await self.client.put(
            f"/tasks/{task.id}",
            json={
                "name": "updated",
                "command": "echo updated",
                "priority": 9,
                "requested_gpus": 2,
                "target_worker": "node-a",
                "env": {"A": "1"},
                "background": True,
                "elastic": True,
                "log_mode": "pipe",
                "im_notify": True,
            },
        )

        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["task"]["command"], "echo updated")
        self.assertEqual(payload["task"]["requested_gpus"], 2)
        self.assertEqual(payload["task"]["target_worker"], "node-a")
        self.assertTrue(payload["task"]["background"])
        self.assertTrue(payload["task"]["preemptible"])

    async def test_favicon_is_served(self) -> None:
        response = await self.client.get("/favicon.ico")

        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, "image/x-icon")
        self.assertEqual((await response.read())[:4], b"\x00\x00\x01\x00")

    async def test_tasks_can_be_searched_by_name_or_id(self) -> None:
        named = self.store.create_task(name="Experiment Alpha", command="true")
        identified = self.store.create_task(name="other", command="true")
        self.store.create_task(name="unrelated", command="true")

        name_response = await self.client.get("/tasks?q=ment%20alp")
        id_response = await self.client.get(f"/tasks?q={identified.id[5:13]}")

        self.assertEqual(name_response.status, 200)
        name_payload = await name_response.json()
        self.assertEqual(name_payload["total"], 1)
        self.assertEqual([task["id"] for task in name_payload["tasks"]], [named.id])
        id_payload = await id_response.json()
        self.assertEqual(id_payload["total"], 1)
        self.assertEqual([task["id"] for task in id_payload["tasks"]], [identified.id])

    async def test_put_rejects_running_task(self) -> None:
        task = self.store.create_task(name="running", command="echo old")
        self.store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-running")

        response = await self.client.put(
            f"/tasks/{task.id}",
            json={"name": "changed", "command": "echo changed"},
        )

        self.assertEqual(response.status, 409)
        payload = await response.json()
        self.assertIn("paused or unstarted queued", payload["error"])
        self.assertEqual(self.store.get_task(task.id).command, "echo old")

    async def test_pause_reports_remote_kill_failure(self) -> None:
        task = self.store.create_task(name="running", command="sleep 10", preemptible=True)
        self.store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-running")
        self.scheduler.remote.kill_task = Mock(side_effect=RuntimeError("worker SSH connection closed"))

        response = await self.client.post(f"/tasks/{task.id}/pause", json={})

        self.assertEqual(response.status, 409)
        payload = await response.json()
        self.assertEqual(payload["error"], "pause failed: worker SSH connection closed")

    async def test_workers_include_runtime_connectivity(self) -> None:
        self.store.record_worker_probe(
            "node-a",
            reachable=False,
            error="Connection closed by node-a port 22",
        )

        response = await self.client.get("/workers")

        self.assertEqual(response.status, 200)
        worker = (await response.json())["workers"][0]
        self.assertEqual(worker["connection_state"], "offline")
        self.assertIsNotNone(worker["last_probe_at"])
        self.assertIn("Connection closed", worker["last_probe_error"])

    async def test_put_worker_persists_and_hot_updates_scheduler(self) -> None:
        response = await self.client.put(
            "/workers/node-a",
            json={
                "host": "10.0.0.8",
                "user": "trainer",
                "port": 2202,
                "enabled": False,
                "tmux_prefix": "jobs",
                "gpus": [2, 3],
                "max_concurrent_tasks": 2,
                "max_background_tasks": 1,
            },
        )

        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["worker"]["host"], "10.0.0.8")
        self.assertEqual(self.scheduler.workers["node-a"].gpus, [2, 3])
        self.assertEqual(self.scheduler.workers["node-a"].ssh_key, "~/.ssh/id_ed25519")
        self.assertFalse(self.scheduler.workers["node-a"].enabled)
        persisted = self.config_path.read_text(encoding="utf-8")
        self.assertIn('host = "10.0.0.8"', persisted)
        self.assertIn("max_concurrent_tasks = 2", persisted)
        self.assertIn('ssh_key = "~/.ssh/id_ed25519"', persisted)

    async def test_put_worker_rejects_name_change(self) -> None:
        response = await self.client.put("/workers/node-a", json={"name": "renamed"})

        self.assertEqual(response.status, 400)
        self.assertIn("name cannot be changed", (await response.json())["error"])

    async def test_log_stream_accepts_zero_tail_bytes_for_reconnect(self) -> None:
        task = self.store.create_task(name="stream", command="echo stream")
        self.store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-stream")
        calls: list[int] = []

        async def open_log_stream(worker, task_id: str, *, tail_bytes: int):
            calls.append(tail_bytes)
            return await asyncio.create_subprocess_exec(
                "bash",
                "-c",
                "printf stream; sleep 0.1",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )

        self.scheduler.remote.open_log_stream = open_log_stream
        websocket = await self.client.ws_connect(f"/tasks/{task.id}/logs/stream?tail_bytes=0")
        message = await websocket.receive()

        self.assertEqual(message.data, b"stream")
        await websocket.close()
        self.assertEqual(calls, [0])


    async def test_nvtop_stream_relays_terminal_output_input_and_resize(self) -> None:
        class FakePtySession:
            def __init__(self) -> None:
                self.output = asyncio.Queue()
                self.output.put_nowait(b"nvtop-screen")
                self.writes: list[bytes] = []
                self.sizes: list[tuple[int, int]] = []
                self.terminated = False

            async def read(self, size: int = 32768) -> bytes:
                return await self.output.get()

            async def write(self, data: bytes) -> None:
                self.writes.append(data)

            def resize(self, cols: int, rows: int) -> None:
                self.sizes.append((cols, rows))

            async def terminate(self) -> None:
                self.terminated = True

        session = FakePtySession()
        opened: list[tuple[str, int, int]] = []

        async def open_nvtop(worker, *, cols: int, rows: int):
            opened.append((worker.name, cols, rows))
            return session

        self.scheduler.remote.open_nvtop = open_nvtop
        websocket = await self.client.ws_connect("/workers/node-a/nvtop/stream?cols=140&rows=42")
        message = await websocket.receive()
        self.assertEqual(message.data, b"nvtop-screen")

        await websocket.send_json({"type": "input", "data": "q"})
        await websocket.send_json({"type": "resize", "cols": 160, "rows": 50})
        for _ in range(20):
            if session.writes and session.sizes:
                break
            await asyncio.sleep(0.01)
        await websocket.close()
        for _ in range(20):
            if session.terminated:
                break
            await asyncio.sleep(0.01)

        self.assertEqual(opened, [("node-a", 140, 42)])
        self.assertEqual(session.writes, [b"q"])
        self.assertEqual(session.sizes, [(160, 50)])
        self.assertTrue(session.terminated)


class AuthApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        worker = WorkerConfig(name="node-a", host="node-a", gpus=[0])
        self.config = AppConfig(
            daemon=DaemonConfig(db_path=str(Path(self.tmp.name) / "auth.db")),
            api=ApiConfig(enabled=True),
            workers=[worker],
            auth=AuthConfig(
                enabled=True,
                username="admin",
                password_hash=hash_password("correct horse battery staple"),
                session_secret="s" * 48,
                login_max_attempts=2,
            ),
        )
        self.store = Store(self.config.daemon.db_path)
        self.store.init()
        self.store.sync_workers(self.config.workers)
        scheduler = Scheduler(self.config, self.store)
        self.client = TestClient(
            TestServer(_make_app(self.config, self.store, scheduler)),
            cookie_jar=CookieJar(unsafe=True),
        )
        await self.client.start_server()
        self.origin = str(self.client.make_url("/")).rstrip("/")

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.tmp.cleanup()

    async def test_requires_authentication_and_redirects_browser_index(self) -> None:
        api_response = await self.client.get("/tasks")
        page_response = await self.client.get("/", allow_redirects=False)

        self.assertEqual(api_response.status, 401)
        self.assertEqual(page_response.status, 302)
        self.assertEqual(page_response.headers["location"], "/login")
        self.assertEqual(api_response.headers["x-frame-options"], "DENY")

    async def test_session_login_requires_csrf_for_mutation(self) -> None:
        response = await self.client.post(
            "/auth/login",
            json={"username": "admin", "password": "correct horse battery staple"},
            headers={"origin": self.origin},
        )
        self.assertEqual(response.status, 200)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=Strict", response.headers["set-cookie"])

        session_response = await self.client.get("/auth/session")
        session = await session_response.json()
        self.assertTrue(session["authenticated"])

        rejected = await self.client.post(
            "/tasks",
            json={"name": "rejected", "command": "true"},
            headers={"origin": self.origin},
        )
        self.assertEqual(rejected.status, 403)

        accepted = await self.client.post(
            "/tasks",
            json={"name": "accepted", "command": "true"},
            headers={"origin": self.origin, "x-csrf-token": session["csrf_token"]},
        )
        self.assertEqual(accepted.status, 201)

    async def test_public_bearer_token_is_not_an_authentication_method(self) -> None:
        response = await self.client.post(
            "/tasks",
            json={"name": "agent", "command": "true"},
            headers={
                "authorization": "Bearer no-longer-supported",
                "x-forwarded-for": "203.0.113.10",
            },
        )

        self.assertEqual(response.status, 401)

    async def test_local_cli_marker_can_call_loopback_api_without_credentials(self) -> None:
        response = await self.client.post(
            "/tasks",
            json={"name": "agent", "command": "true"},
            headers={LOCAL_CLI_HEADER: "1"},
        )

        self.assertEqual(response.status, 201)

    async def test_proxied_cli_marker_does_not_bypass_web_login(self) -> None:
        response = await self.client.get(
            "/tasks",
            headers={LOCAL_CLI_HEADER: "1", "x-forwarded-for": "203.0.113.10"},
        )

        self.assertEqual(response.status, 401)

    async def test_login_failures_are_rate_limited(self) -> None:
        for _ in range(2):
            response = await self.client.post(
                "/auth/login",
                json={"username": "admin", "password": "wrong password"},
                headers={"origin": self.origin},
            )
            self.assertEqual(response.status, 401)

        limited = await self.client.post(
            "/auth/login",
            json={"username": "admin", "password": "wrong password"},
            headers={"origin": self.origin},
        )
        self.assertEqual(limited.status, 429)
        self.assertIn("retry-after", limited.headers)


if __name__ == "__main__":
    unittest.main()
