"""静态守卫：UI 测试必须静默运行，绝不抢真实鼠标 / 弹出真实窗口。

禁词清单（逐条维护，新增禁词只需加进 ``BANNED``）：
- ``QTest.mouseMove``          —— 任何形式都会移动系统光标；
- ``QCursor.setPos``           —— 直接定位系统光标；
- ``setOverrideCursor``        —— 会改写全局光标状态（含 QCursor. 前缀形式）；
- ``move(-5000``               —— “把窗口移出屏幕”是假静默，仍会占用真实窗口；
- 未经 ``--real`` 的真实平台    —— ``QT_QPA_PLATFORM`` 在本目录的非诊断测试里
  只允许赋值为 ``offscreen``。

另外两条结构规则：
- 文件里只要出现 ``.show()``，就必须同时存在 ``WA_DontShowOnScreen``；
- 含 Qt 的测试模块必须出现 ``offscreen`` 字样，确保平台在子进程环境里被固定。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
GUARD_NAME = Path(__file__).name

BANNED = (
    ("QTest.mouseMove", "QTest.mouseMove 会移动系统光标"),
    ("QCursor.setPos", "QCursor.setPos 会直接定位系统光标"),
    ("setOverrideCursor", "setOverrideCursor 会改写全局光标状态"),
    ("move(-5000", "把窗口移出屏幕是假静默，仍会占用真实窗口"),
)

PLATFORM_ASSIGN = (
    re.compile(r"""QT_QPA_PLATFORM["'\]]*\s*[:=]\s*["']([^"']+)["']"""),
    re.compile(
        r"""setdefault\(\s*["']QT_QPA_PLATFORM["']\s*,\s*["']([^"']+)["']\)"""
    ),
)


def scan_files() -> list[Path]:
    """全部测试与 UI 用例文件（守卫自身与诊断脚本另行处理）。"""
    return sorted(
        path
        for path in TESTS_DIR.glob("*.py")
        if (path.name.startswith("test_") or path.name.endswith("_cases.py"))
        and path.name != GUARD_NAME
    )


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class TestHygieneGuard(unittest.TestCase):
    def test_guard_scans_expected_ui_files(self) -> None:
        names = {path.name for path in scan_files()}
        for expected in (
            "test_ui_qt.py",
            "qt_main_cases.py",
            "qt_workbench_cases.py",
            "qt_review_cases.py",
            "test_review_workbench.py",
        ):
            self.assertIn(expected, names)

    def test_forbidden_cursor_tokens_absent(self) -> None:
        for path in scan_files():
            text = read(path)
            for token, reason in BANNED:
                with self.subTest(file=path.name, token=token):
                    self.assertNotIn(token, text, f"{path.name}: {reason}")

    def test_platform_assignments_are_offscreen_only(self) -> None:
        for path in scan_files():
            text = read(path)
            for pattern in PLATFORM_ASSIGN:
                for value in pattern.findall(text):
                    with self.subTest(file=path.name, value=value):
                        self.assertEqual(
                            value,
                            "offscreen",
                            f"{path.name}: QT_QPA_PLATFORM 只允许 offscreen",
                        )

    def test_ui_modules_declare_offscreen(self) -> None:
        for path in scan_files():
            text = read(path)
            if "from PySide6" not in text and "import PySide6" not in text:
                continue
            with self.subTest(file=path.name):
                self.assertIn(
                    "offscreen",
                    text,
                    f"{path.name}: UI 测试模块必须显式声明 offscreen",
                )

    def test_show_requires_dont_show_on_screen(self) -> None:
        for path in scan_files():
            text = read(path)
            if ".show()" not in text:
                continue
            with self.subTest(file=path.name):
                self.assertIn(
                    "WA_DontShowOnScreen",
                    text,
                    f"{path.name}: 调用 show() 时必须设置 WA_DontShowOnScreen",
                )

    def test_tests_never_auto_run_diag_scripts(self) -> None:
        for path in scan_files():
            for line_number, line in enumerate(read(path).splitlines(), 1):
                if "diag_" not in line:
                    continue
                # 允许注释里提到诊断脚本；只禁止真正的导入/子进程调用。
                if any(
                    marker in line
                    for marker in ("import ", "subprocess", "Popen", ".run(", "check_")
                ):
                    with self.subTest(file=path.name, line=line_number):
                        self.fail(
                            f"{path.name}:{line_number} 在测试里调用诊断脚本：{line.strip()}"
                        )

    def test_diag_scripts_default_offscreen_and_require_real_flag(self) -> None:
        for path in sorted(TESTS_DIR.glob("diag_*.py")):
            text = read(path)
            with self.subTest(file=path.name):
                self.assertIn("--real", text, "诊断脚本必须以 --real 显式选择真实平台")
                self.assertIn("offscreen", text, "诊断脚本默认必须是 offscreen")
                self.assertIn(
                    "QT_QPA_PLATFORM", text, "诊断脚本需显式设置平台"
                )
                self.assertTrue(
                    "警告" in text or "桌面" in text,
                    "诊断脚本需提示 --real 会弹窗/占用桌面",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
