"""Qt-only 启动测试：入口不可回退、不得引入旧 GUI 框架、缺失依赖需明确报错。"""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BOOT_SCRIPT = r"""
import sys
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow

sys.path.insert(0, r"{root}")
import main as app_main
import review_qt

app = review_qt.ensure_qapplication()
state = {{"windows": 0, "main_windows": 0, "tk_root": False}}


def inspect_and_quit():
    state["windows"] = len(app.topLevelWidgets())
    state["main_windows"] = sum(
        1 for widget in app.topLevelWidgets() if isinstance(widget, QMainWindow)
    )
    state["tk_root"] = "tkinter" in sys.modules
    app.quit()


QTimer.singleShot(300, inspect_and_quit)
code = app_main._run_qt()
assert code == 0, code
assert state["windows"] >= 1, state
assert state["main_windows"] >= 1, state
assert state["tk_root"] is False, state
print("OK smoke")
"""

MISSING_SCRIPT = r"""
import sys


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name == "PySide6" or name.startswith("PySide6."):
            raise ImportError("PySide6 被人为屏蔽")
        return None


sys.meta_path.insert(0, Blocker())
sys.path.insert(0, r"{root}")
import main as app_main

code = app_main.main()
assert "tkinter" not in sys.modules
print(f"EXIT={{code}}")
raise SystemExit(code)
"""


def _run(code: str, timeout: float = 20.0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["QT_QPA_PLATFORM"] = "offscreen"
    return subprocess.run(
        [sys.executable, "-c", code.format(root=str(PROJECT_ROOT))],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class QtOnlyEntryTest(unittest.TestCase):
    def test_import_main_does_not_load_legacy_gui(self) -> None:
        result = _run(
            "import sys; sys.path.insert(0, r'{root}'); import main; "
            "assert 'tkinter' not in sys.modules; "
            "assert '_tkinter' not in sys.modules; print('OK import')"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK import", result.stdout)

    def test_smoke_main_window_is_qmainwindow_and_has_no_tk_root(self) -> None:
        result = _run(BOOT_SCRIPT, timeout=25.0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK smoke", result.stdout)

    def test_missing_pyside6_exits_nonzero_with_clear_error(self) -> None:
        result = _run(MISSING_SCRIPT, timeout=20.0)
        self.assertNotEqual(result.returncode, 0, "缺失 PySide6 时必须以非零码退出")
        self.assertIn("PySide6", result.stderr)
        self.assertIn("pip install -r requirements.txt", result.stderr)
        self.assertIn("EXIT=1", result.stdout)
        self.assertNotIn("tkinter", result.stderr.lower())

    def test_main_delegates_to_qt_without_fallback(self) -> None:
        sys.path.insert(0, str(PROJECT_ROOT))
        import main as app_main

        with mock.patch.object(app_main, "prepare_windows_shell", return_value="ok") as shell:
            with mock.patch.object(app_main, "_run_qt", return_value=0) as run_qt:
                self.assertEqual(app_main.main(), 0)
        shell.assert_called_once()
        run_qt.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
