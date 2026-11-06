"""打包参数 dry-run 断言：不实际运行 PyInstaller。

确保 Qt 运行时/平台插件进入产物，同时旧的排除项已移除，
并且保留图标、add-data 与同版本产物名。
"""

from __future__ import annotations

import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
BAT = PROJECT_ROOT / "build_onefile.bat"
ONEDIR_BAT = PROJECT_ROOT / "build_onedir.bat"
SPEC = PROJECT_ROOT / "PixFilter.spec"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class BuildQtTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bat = read(BAT)
        self.onedir_bat = read(ONEDIR_BAT)
        self.spec = read(SPEC)
        self.req = read(REQUIREMENTS)

    def test_requirements_has_pyside6_range(self) -> None:
        self.assertIn("PySide6>=6.6,<7", self.req)

    def test_bat_no_longer_excludes_qt(self) -> None:
        for token in ('--exclude-module "PySide6"', '--exclude-module "shiboken6"'):
            self.assertNotIn(token, self.bat)

    def test_bat_collects_qt_runtime_and_plugins(self) -> None:
        self.assertIn('--collect-all "PySide6"', self.bat)
        self.assertIn('--collect-all "shiboken6"', self.bat)
        for module in ("QtCore", "QtGui", "QtWidgets", "QtSvg"):
            self.assertIn(f'--hidden-import "PySide6.{module}"', self.bat)
        self.assertIn("PySide6.QtSvg", self.bat)
        self.assertIn("PySide6.QtWidgets", self.bat)

    def test_bat_keeps_icon_data_and_output_name(self) -> None:
        self.assertIn('--name "PixFilter_v1.0.3"', self.bat)
        self.assertIn('--icon "PixFilter.ico"', self.bat)
        self.assertIn('--add-data "PixFilter.ico;."', self.bat)
        self.assertIn("--onefile", self.bat)
        self.assertIn("--windowed", self.bat)
        self.assertIn('set "OUTPUT=%DIST_DIR%\\PixFilter_v1.0.3.exe"', self.bat)

    def test_onefile_cleanup_only_replaces_current_exe(self) -> None:
        self.assertIn('set "OUTPUT=%DIST_DIR%\\PixFilter_v1.0.3.exe"', self.bat)
        self.assertIn('del /f /q "%OUTPUT%"', self.bat)
        self.assertNotIn('rmdir /s /q "%DIST_DIR%"', self.bat)
        self.assertNotIn('del /f /q "%DIST_DIR%\\PixFilter_v1.0.exe"', self.bat)
        self.assertNotIn('del /f /q "%DIST_DIR%\\PixFilter_v1.0.1.exe"', self.bat)

    def test_onedir_uses_folder_output_and_scoped_cleanup(self) -> None:
        self.assertIn("--onedir", self.onedir_bat)
        self.assertIn('--windowed', self.onedir_bat)
        self.assertIn('--name "PixFilter_v1.0.3"', self.onedir_bat)
        self.assertIn('--icon "PixFilter.ico"', self.onedir_bat)
        self.assertIn('--add-data "PixFilter.ico;."', self.onedir_bat)
        self.assertIn('set "DIST_DIR=%ROOT%dist_onedir"', self.onedir_bat)
        self.assertIn('set "FINAL_DIR=%DIST_DIR%\\PixFilter_v1.0.3_folder"', self.onedir_bat)
        self.assertIn(
            'set "OUTPUT=%FINAL_DIR%\\PixFilter_v1.0.3.exe"', self.onedir_bat
        )
        self.assertIn(
            'ren "%APP_DIR%" "PixFilter_v1.0.3_folder"', self.onedir_bat
        )
        self.assertIn('rmdir /s /q "%FINAL_DIR%"', self.onedir_bat)
        self.assertIn('rmdir /s /q "%APP_DIR%"', self.onedir_bat)
        self.assertNotIn('rmdir /s /q "%DIST_DIR%"', self.onedir_bat)
        self.assertNotIn('rmdir /s /q "dist"', self.onedir_bat)

    def test_spec_files_do_not_collide(self) -> None:
        self.assertIn('set "SPEC_FILE=%ROOT%PixFilter.spec"', self.bat)
        self.assertIn('set "SPEC_FILE=%ROOT%PixFilter_onedir.spec"', self.onedir_bat)

    def test_dependency_check_includes_qt(self) -> None:
        self.assertIn("PySide6.QtCore", self.bat)
        self.assertIn("PySide6.QtSvg", self.bat)

    def test_bat_has_no_legacy_gui_imports_and_excludes_it(self) -> None:
        self.assertNotIn('--hidden-import "tkinter"', self.bat)
        self.assertNotIn("PIL.ImageTk", self.bat)
        self.assertNotIn("PIL._tkinter_finder", self.bat)
        dep_line = next(line for line in self.bat.splitlines() if "DEP_CHECK=" in line)
        self.assertNotIn("tkinter", dep_line)
        self.assertIn('--exclude-module "tkinter"', self.bat)
        self.assertIn('--exclude-module "_tkinter"', self.bat)

    def test_bats_are_ascii_no_bom_and_crlf(self) -> None:
        for path in (BAT, ONEDIR_BAT):
            data = path.read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"), f"{path.name} has a BOM")
            self.assertLessEqual(max(data), 127, f"{path.name} is not pure ASCII")
            self.assertEqual(
                data.count(b"\r\n"), data.count(b"\n"), f"{path.name} has bare LF"
            )
            self.assertEqual(
                data.count(b"\r\n"), data.count(b"\r"), f"{path.name} has bare CR"
            )

    def test_requirements_have_no_legacy_backend(self) -> None:
        self.assertNotIn("tkinter", self.req)
        self.assertNotIn("PIXFILTER_UI", self.req)

    def test_spec_excludes_legacy_gui(self) -> None:
        excludes = self.spec.split("excludes=")[1].split("]")[0]
        self.assertIn("'tkinter'", excludes)
        self.assertIn("'_tkinter'", excludes)
        self.assertNotIn("'tkinter'", self.spec.split("hiddenimports =")[1].split("]")[0])

    def test_spec_matches_cli(self) -> None:
        self.assertIn("collect_all('PySide6')", self.spec)
        self.assertIn("collect_all('shiboken6')", self.spec)
        self.assertIn("'PySide6.QtSvg'", self.spec)
        self.assertNotIn("'PySide6'", self.spec.split("excludes=")[1].split("]")[0])
        self.assertNotIn("'shiboken6'", self.spec.split("excludes=")[1].split("]")[0])
        self.assertIn("('PixFilter.ico', '.')", self.spec)
        self.assertIn("name='PixFilter_v1.0.3'", self.spec)
        # PyInstaller 6.22 生成 icon=['PixFilter.ico']，旧版本为 icon='PixFilter.ico'。
        self.assertRegex(self.spec, r"icon=\[?'PixFilter\.ico'\]?")


if __name__ == "__main__":
    unittest.main(verbosity=2)
