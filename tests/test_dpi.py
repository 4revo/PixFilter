"""高 DPI 缩放自测：全部使用隐藏窗口（withdraw + alpha 0），不弹可见界面。

运行：python tests/test_dpi.py -v
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tkinter as tk
from tkinter import font as tkfont

import main as app_main


class DpiScalingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # 与真实入口一致：必须在任何 tk.Tk() 之前启用 DPI 感知。
        cls.awareness = app_main._enable_windows_dpi_awareness()

    def _hidden_root(self) -> tk.Tk:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-alpha", 0.0)
        except tk.TclError:
            pass
        return root

    def test_00_awareness_is_called_without_crash(self) -> None:
        self.assertIsInstance(self.awareness, str)
        self.assertTrue(self.awareness.startswith(("shcore", "user32", "skip")))

    def test_real_dpi_scaling_fonts_and_widgets(self) -> None:
        root = self._hidden_root()
        started = time.perf_counter()
        try:
            app_main.configure_dpi_scaling(root)
            dpi = float(root.winfo_fpixels("1i"))
            scaling = float(root.tk.call("tk", "scaling"))
            default_size = int(tkfont.nametofont("TkDefaultFont").cget("size"))
            heading_size = int(tkfont.nametofont("TkHeadingFont").cget("size"))
            button = tk.Button(root, text="开始扫描", font=("Microsoft YaHei UI", 10))
            button.pack()
            root.update_idletasks()
            button_height = button.winfo_reqheight()
            button_width = button.winfo_reqwidth()
        finally:
            root.destroy()

        elapsed = time.perf_counter() - started
        print(
            f"\nDPI awareness={self.awareness}, dpi={dpi:.0f}, "
            f"tk scaling={scaling:.3f}, TkDefaultFont={default_size}pt, "
            f"button={button_width}x{button_height}, elapsed={elapsed:.2f}s"
        )
        self.assertLess(elapsed, 3.0)
        self.assertGreater(scaling, 1.0)
        self.assertGreaterEqual(default_size, 10)
        self.assertGreaterEqual(heading_size, 10)
        # 防御：字体/缩放处理不得把按钮压成 1px。
        self.assertGreater(button_height, 20)
        self.assertGreater(button_width, 40)

    def test_simulated_200_percent_dpi_math(self) -> None:
        """本机若是 100% 缩放，用假的 DPI 值间接验证换算逻辑。"""
        root = self._hidden_root()
        try:
            with mock.patch.object(root, "winfo_fpixels", return_value=192.0):
                scale = app_main.configure_dpi_scaling(root)
            scaling = float(root.tk.call("tk", "scaling"))
        finally:
            root.destroy()

        self.assertAlmostEqual(scale, 2.0, places=3)
        self.assertAlmostEqual(scaling, 192.0 / 72.0, places=3)

    def test_hidden_app_builds_with_reasonable_controls(self) -> None:
        root = self._hidden_root()
        app = None
        started = time.perf_counter()
        try:
            app_main.configure_dpi_scaling(root)
            app = app_main.PixFilterApp(root)
            root.update_idletasks()
            elapsed = time.perf_counter() - started
            start_height = app.btn_start.winfo_reqheight()
            start_width = app.btn_start.winfo_reqwidth()
            log_height = app.log_text.winfo_reqheight()
            skip_color = app.log_text.tag_cget("skip", "foreground")
            ui_scale = app._ui_scale
        finally:
            if app is not None:
                app._closed = True
            root.destroy()

        print(
            f"\nui_scale={ui_scale:.2f}, start={start_width}x{start_height}, "
            f"log_height={log_height}, elapsed={elapsed:.2f}s"
        )
        self.assertLess(elapsed, 3.0)
        self.assertGreaterEqual(ui_scale, 1.0)
        self.assertGreater(start_height, 20)
        self.assertGreater(start_width, 60)
        self.assertGreater(log_height, 100)
        self.assertEqual(skip_color, "#C62828")


if __name__ == "__main__":
    unittest.main(verbosity=2)
