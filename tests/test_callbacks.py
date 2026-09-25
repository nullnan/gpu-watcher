from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from gpu_watcher.callbacks import run_task_callback
from gpu_watcher.store import Store


class CallbackTests(unittest.TestCase):
    def test_run_task_callback_renders_title_and_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "callback.txt"
            store = Store(Path(tmp) / "test.db")
            store.init()
            task = store.create_task(
                name="notify",
                command="echo notify",
                callback_command=f"printf '%s\\n%s\\n' {{title}} {{content}} > {output}",
            )

            result = run_task_callback(task, "succeeded", "done")

            self.assertIsNotNone(result)
            self.assertEqual(result.returncode, 0)
            text = output.read_text(encoding="utf-8")
            self.assertIn("gpu-watcher succeeded: notify", text)
            self.assertIn(f"id={task.id}", text)
            self.assertIn("detail=done", text)


if __name__ == "__main__":
    unittest.main()
