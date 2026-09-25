from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from gpu_watcher.cli import LOCAL_CLIENT_HEADER, _api_headers, main
from gpu_watcher.store import Store


class CliTests(unittest.TestCase):
    def test_api_headers_identify_the_local_cli_without_credentials(self) -> None:
        headers = _api_headers()

        self.assertEqual(headers[LOCAL_CLIENT_HEADER], "1")
        self.assertNotIn("authorization", headers)

    def test_submit_and_list_with_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")

            with redirect_stdout(io.StringIO()) as output:
                main(["submit", "--db", db, "--name", "cli", "--priority", "5", "--", "echo hello"])
            task = json.loads(output.getvalue())

            self.assertEqual(task["name"], "cli")
            self.assertEqual(task["priority"], 5)

            with redirect_stdout(io.StringIO()) as output:
                main(["tasks", "--db", db])
            self.assertIn("cli", output.getvalue())

    def test_show_with_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            with redirect_stdout(io.StringIO()) as output:
                main(["submit", "--db", db, "--name", "detail", "--", "echo detail"])
            task_id = json.loads(output.getvalue())["id"]

            with redirect_stdout(io.StringIO()) as output:
                main(["show", "--db", db, "--json", task_id])

            self.assertEqual(json.loads(output.getvalue())["id"], task_id)

    def test_submit_paused_then_start_with_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            with redirect_stdout(io.StringIO()) as output:
                main(["submit", "--db", db, "--paused", "--name", "held", "--", "echo held"])
            task = json.loads(output.getvalue())
            self.assertEqual(task["status"], "paused")

            with redirect_stdout(io.StringIO()) as output:
                main(["start", "--db", db, task["id"]])

            self.assertIn(f"start {task['id']}", output.getvalue())

    def test_submit_callback_options_with_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")

            with redirect_stdout(io.StringIO()) as output:
                main([
                    "submit",
                    "--db",
                    db,
                    "--callback-command",
                    "notify {title} {content}",
                    "--callback-on",
                    "complete,failed",
                    "--callback-on",
                    "switch",
                    "--",
                    "echo notify",
                ])
            task = json.loads(output.getvalue())

            self.assertEqual(task["callback_command"], "notify {title} {content}")
            self.assertEqual(task["callback_events"], ["succeeded", "failed", "switch"])

    def test_submit_im_notify_with_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")

            with redirect_stdout(io.StringIO()) as output:
                main(["submit", "--db", db, "--im-notify", "--", "echo notify"])
            task = json.loads(output.getvalue())

            self.assertTrue(task["im_notify"])

    def test_submit_persists_cron_schedule_options(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            with redirect_stdout(io.StringIO()) as output:
                main([
                    "submit", "--db", db, "--paused",
                    "--start-cron", "0 9 * * mon-fri",
                    "--stop-cron", "0 18 * * mon-fri",
                    "--schedule-timezone", "Asia/Shanghai",
                    "--", "sleep 3600",
                ])
            task = json.loads(output.getvalue())

        self.assertEqual(task["start_cron"], "0 9 * * mon-fri")
        self.assertEqual(task["stop_cron"], "0 18 * * mon-fri")
        self.assertEqual(task["schedule_timezone"], "Asia/Shanghai")
        self.assertEqual(task["status"], "paused")

    def test_pause_and_terminate_with_db(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            with redirect_stdout(io.StringIO()) as output:
                main(["submit", "--db", db, "--preemptible", "--name", "hold", "--", "echo hold"])
            task_id = json.loads(output.getvalue())["id"]

            with redirect_stdout(io.StringIO()) as output:
                main(["pause", "--db", db, task_id])
            self.assertIn(f"pause {task_id}", output.getvalue())

            with redirect_stdout(io.StringIO()) as output:
                main(["terminate", "--db", db, task_id])
            self.assertIn(f"terminate {task_id}", output.getvalue())

    def test_rerun_with_api_url_posts_to_daemon(self) -> None:
        with patch("gpu_watcher.cli._api_post") as api_post:
            api_post.return_value = {"ok": True}

            with redirect_stdout(io.StringIO()) as output:
                main(["rerun", "--api-url", "http://daemon:8765", "task-1"])

        api_post.assert_called_once_with("http://daemon:8765", "/tasks/task-1/rerun", {})
        self.assertEqual(output.getvalue(), "rerun task-1\n")

    def test_logs_with_api_url_prints_daemon_logs(self) -> None:
        with patch("gpu_watcher.cli._api_get") as api_get:
            api_get.return_value = {"logs": "hello\nfrom daemon\n"}

            with redirect_stdout(io.StringIO()) as output:
                main(["logs", "--api-url", "http://daemon:8765", "--lines", "20", "task-1"])

        api_get.assert_called_once_with("http://daemon:8765", "/tasks/task-1/logs?lines=20")
        self.assertEqual(output.getvalue(), "hello\nfrom daemon\n\n")

    def test_wait_with_db_prints_terminal_task_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            store = Store(db)
            store.init()
            task = store.create_task(name="done", command="echo done")
            store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-test")
            store.finish_task(task.id, exit_code=0)

            with redirect_stdout(io.StringIO()) as output:
                main(["wait", "--db", db, "--json", "--interval", "0.01", "--timeout", "1", task.id])

            waited = json.loads(output.getvalue())
            self.assertEqual(waited["id"], task.id)
            self.assertEqual(waited["status"], "succeeded")

    def test_wait_with_db_uses_failed_task_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "test.db")
            store = Store(db)
            store.init()
            task = store.create_task(name="fail", command="false")
            store.mark_running(task.id, worker_name="node-a", gpu_indexes=[0], tmux_session="gw-test")
            store.finish_task(task.id, exit_code=7)

            with redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as raised:
                    main(["wait", "--db", db, "--json", "--interval", "0.01", task.id])

            self.assertEqual(raised.exception.code, 7)
            self.assertEqual(json.loads(output.getvalue())["status"], "failed")

    def test_wait_with_api_url_polls_until_terminal(self) -> None:
        with patch("gpu_watcher.cli._api_get") as api_get:
            api_get.side_effect = [
                {"task": {"id": "task-1", "status": "running"}},
                {"task": {"id": "task-1", "status": "succeeded", "exit_code": 0}},
            ]

            with redirect_stdout(io.StringIO()) as output:
                main(["wait", "--api-url", "http://daemon:8765", "--json", "--interval", "0.01", "task-1"])

        self.assertEqual(json.loads(output.getvalue())["status"], "succeeded")
        self.assertEqual(api_get.call_count, 2)

    def test_submit_wait_prints_final_task(self) -> None:
        with patch("gpu_watcher.cli._api_post") as api_post, patch("gpu_watcher.cli._wait_for_task") as wait_for_task:
            api_post.return_value = {"task": {"id": "task-1", "status": "queued"}}
            wait_for_task.return_value = {"id": "task-1", "status": "succeeded", "exit_code": 0}

            with redirect_stdout(io.StringIO()) as output:
                main([
                    "submit",
                    "--api-url",
                    "http://daemon:8765",
                    "--wait",
                    "--wait-interval",
                    "0.01",
                    "--",
                    "echo done",
                ])

        self.assertEqual(json.loads(output.getvalue())["status"], "succeeded")
        wait_for_task.assert_called_once()


if __name__ == "__main__":
    unittest.main()
