"""图标集成无 GUI 测试：ICO 内容、AppUserModelID、spec 与源码约束。

Qt 侧的 icon/resource_path 行为由 tests/qt_main_cases.py 的 case a 覆盖。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image

import main as app_main

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class IconIntegrationTest(unittest.TestCase):
    def test_root_icon_exists_and_contains_required_sizes(self) -> None:
        icon = PROJECT_ROOT / "PixFilter.ico"
        self.assertTrue(icon.is_file())
        with icon.open("rb") as handle:
            header = handle.read(4)
        self.assertEqual(header, b"\x00\x00\x01\x00")
        with Image.open(icon) as image:
            sizes = set(image.ico.sizes())
        for size in (16, 32, 48, 256):
            self.assertIn((size, size), sizes)

    def test_prepare_windows_shell_returns_status(self) -> None:
        result = app_main.prepare_windows_shell()
        self.assertIn(
            result, {"ok", "failed", "skip:non-windows", "skip:no-windll"}
        )

    def test_no_iconbitmap_calls_remain(self) -> None:
        for name in (
            "main.py",
            "review_qt.py",
            "review_workbench_qt.py",
            "ui_qt_main.py",
            "scan_service.py",
        ):
            source = (PROJECT_ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("iconbitmap(", source, name)

    def test_spec_contains_icon_and_datas_when_present(self) -> None:
        spec = PROJECT_ROOT / "PixFilter.spec"
        if not spec.exists():
            self.skipTest("spec 由 PyInstaller 自动生成，当前不存在")
        text = spec.read_text(encoding="utf-8")
        self.assertIn("('PixFilter.ico', '.')", text)
        # PyInstaller 6.22 生成 icon=['PixFilter.ico']，旧版本为 icon='PixFilter.ico'。
        self.assertRegex(text, r"icon=\[?'PixFilter\.ico'\]?")
        self.assertIn("name='PixFilter_v1.0.3'", text)
        self.assertFalse((PROJECT_ROOT / "PixFilter_v1.0.spec").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
