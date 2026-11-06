"""架构约束测试：依赖方向、UI 边界与配置引用。

全部使用 AST / 文本断言，不启动任何窗口。项目已彻底移除 Tk 后端，
因此本测试只盘点 Qt 模块；此文件中的 GUI 框架字样仅用于“禁止出现”断言。
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

MODULES = {
    "config",
    "qt_controller",
    "review_qt",
    "review_workbench_qt",
    "ui_qt_main",
    "scan_service",
    "platform_support",
    "main",
    "pixfilter_core",
}
# 单向依赖：
#   config ← review_qt ← review_workbench_qt / ui_qt_main ← main
#   scan_service 只依赖 pixfilter_core，任何 UI 模块都不得被它反向依赖。
ALLOWED_IMPORTS = {
    "config": set(),
    "pixfilter_core": set(),
    "scan_service": {"pixfilter_core"},
    "qt_controller": {"config", "scan_service"},
    "review_qt": {"config", "pixfilter_core"},
    "review_workbench_qt": {"config", "pixfilter_core", "review_qt"},
    "ui_qt_main": {
        "config",
        "pixfilter_core",
        "review_qt",
        "review_workbench_qt",
        "scan_service",
        "qt_controller",
    },
    "platform_support": {"config"},
    "main": {"platform_support", "ui_qt_main"},
}


def _read(name: str) -> str:
    return (PROJECT_ROOT / name).read_text(encoding="utf-8")


def _imports_of(name: str) -> set[str]:
    tree = ast.parse((PROJECT_ROOT / f"{name}.py").read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in MODULES:
                    found.add(root)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                root = node.module.split(".")[0]
                if root in MODULES:
                    found.add(root)
    found.discard(name)
    return found


def _has_cycle(graph: dict[str, set[str]]) -> bool:
    visiting: set[str] = set()
    done: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in done:
            return False
        visiting.add(node)
        for nxt in graph.get(node, set()):
            if visit(nxt):
                return True
        visiting.remove(node)
        done.add(node)
        return False

    return any(visit(node) for node in graph)


class ArchitectureTest(unittest.TestCase):
    def test_a_import_graph_is_acyclic_and_one_way(self) -> None:
        graph = {name: _imports_of(name) for name in MODULES}
        self.assertFalse(_has_cycle(graph), f"存在循环依赖：{graph}")
        for name, allowed in ALLOWED_IMPORTS.items():
            self.assertEqual(
                graph[name], allowed, f"{name}.py 依赖超出允许方向：{graph[name]}"
            )

    def test_b_scan_service_stays_ui_free(self) -> None:
        source = _read("scan_service.py")
        tree = ast.parse(source)

        # 1) import 层面：不得出现 GUI 框架。
        imported_modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.append(node.module)
        for module in imported_modules:
            for token in ("tkinter", "ImageTk", "PySide", "PySide6", "PyQt", "shiboken"):
                self.assertNotIn(
                    token, module, f"scan_service 不允许依赖 GUI：{module}"
                )

        # 2) 文本层面：服务层不得出现任何 UI 符号。
        for token in ("tkinter", "ImageTk", "Tk(", "tk.Tk", "root.after", "PIXFILTER_UI"):
            self.assertNotIn(token, source, f"scan_service 出现 UI 残留：{token}")

        # 3) AST 名称层面：禁止 UI 控件/变量类型。
        forbidden_attrs = {
            "after",
            "after_cancel",
            "StringVar",
            "DoubleVar",
            "ImageTk",
            "Tk",
            "tkinter",
            "winfo_width",
            "winfo_height",
            "configure",
            "clipboard_append",
        }
        forbidden_names = {"tk", "Tk", "StringVar", "DoubleVar", "ImageTk"}
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                hits.append(f"Attr:{node.attr}")
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                hits.append(f"Name:{node.id}")
        self.assertEqual(hits, [], f"scan_service 触碰了 UI 符号：{hits}")

    def test_c_main_entry_is_small_and_layout_free(self) -> None:
        source = _read("main.py")
        self.assertLessEqual(len(source.splitlines()), 120)
        for token in (
            "pack(",
            "grid(",
            "place(",
            "QPushButton",
            "QMainWindow",
            "QWidget",
        ):
            self.assertNotIn(token, source, f"main.py 不应包含布局代码：{token}")
        self.assertIn("prepare_windows_shell", source)
        self.assertIn("PySide6", source)
        self.assertIn("ui_qt_main", source)

    def test_d_qt_ui_files_have_no_hardcoded_colors(self) -> None:
        ui_files = ("review_qt.py", "review_workbench_qt.py", "ui_qt_main.py", "qt_controller.py")
        hex_color = re.compile(r"#[0-9a-fA-F]{6}")
        for name in ui_files:
            self.assertIsNone(
                hex_color.search(_read(name)), f"{name} 仍有硬编码颜色"
            )

    def test_e_config_constants_are_actually_used(self) -> None:
        qt_source = _read("ui_qt_main.py")
        review_source = _read("review_qt.py")
        workbench_source = _read("review_workbench_qt.py")
        self.assertIn("GITHUB_URL", qt_source)
        self.assertIn("GITHUB_URL", _read("qt_controller.py"))
        self.assertIn("QT_BUTTON_RADIUS", qt_source)
        self.assertIn("QT_SCROLLBAR_", review_source)
        self.assertIn("WORKBENCH_", workbench_source)

    def test_f_qt_layer_has_no_legacy_backend_and_no_core_logic(self) -> None:
        """Qt 主界面只做展示编排：不得导入旧后端，也不得直接做核心文件操作。"""
        source = _read("ui_qt_main.py")
        for token in ("tkinter", "ImageTk", "PIXFILTER_UI", "app_controller", "ui_main"):
            self.assertNotIn(token, source)
        for token in (
            "scan_folder(",
            "apply_review_decision(",
            "shutil",
            "hashlib",
            "move_to_trash",
        ):
            self.assertNotIn(token, source, f"Qt 层不应直接实现核心逻辑：{token}")
        imports = _imports_of("ui_qt_main")
        self.assertNotIn("app_controller", imports)
        self.assertNotIn("ui_main", imports)
        # 复核入口只允许批量工作台，主界面不得再走单组弹窗路径。
        for token in ("QtReviewDialog", "show_review_dialog", "close_review_dialog"):
            self.assertNotIn(token, source, f"主界面仍调用单组弹窗：{token}")
        # 视图不再读取业务状态，也不再有编排判定。
        for token in (
            "pending_groups",
            "has_undo",
            "undo_stack",
            "cancel_event",
            "progress_snapshot",
            "group_members",
        ):
            self.assertNotIn(
                token, source, f"ui_qt_main 不应读取业务状态：{token}"
            )

    def test_g_removed_modules_leave_no_source_files(self) -> None:
        for name in ("app_controller.py", "ui_main.py", "review_ui.py", "widgets.py"):
            self.assertFalse(
                (PROJECT_ROOT / name).exists(), f"旧模块仍在仓库中：{name}"
            )

    def test_h_controller_is_pure_python(self) -> None:
        """控制器必须与 Qt 完全解耦：无 GUI import、无 Qt 符号、依赖白名单。"""
        source = _read("qt_controller.py")
        tree = ast.parse(source)

        # 1) import 层面：禁止任何 Qt 绑定。
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules = [node.module]
            else:
                continue
            for module in modules:
                for token in ("PySide", "PySide6", "PyQt", "shiboken"):
                    self.assertNotIn(
                        token, module, f"qt_controller 不允许依赖 Qt：{module}"
                    )

        # 2) 依赖白名单只有 config / scan_service。
        self.assertEqual(
            _imports_of("qt_controller"), {"config", "scan_service"}
        )

        # 3) AST 名称层面：禁止一切 Qt 类型/机制。
        forbidden_names = {
            "QWidget",
            "QMainWindow",
            "QApplication",
            "QDialog",
            "QTimer",
            "Signal",
            "Slot",
            "Qt",
            "QPixmap",
            "QMessageBox",
            "PySide6",
            "shiboken6",
        }
        hits: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                hits.append(f"Name:{node.id}")
            if isinstance(node, ast.Attribute) and node.attr in forbidden_names:
                hits.append(f"Attr:{node.attr}")
        self.assertEqual(hits, [], f"qt_controller 触碰了 Qt 符号：{hits}")

        # 4) 不 import 视图，也不直接触碰控件属性/样式。
        self.assertNotIn("from ui_qt_main", source)
        self.assertNotIn("import ui_qt_main", source)
        for token in ("setEnabled", "setText", "setStyleSheet", "btn_", "pending_tree"):
            self.assertNotIn(
                token, source, f"qt_controller 不应直接操作控件：{token}"
            )

    def test_i_state_decisions_live_only_in_controller(self) -> None:
        """视图不再包含状态判定；判定函数只存在于 qt_controller。"""
        ui_source = _read("ui_qt_main.py")
        controller_source = _read("qt_controller.py")
        ui_defs = {
            node.name
            for node in ast.walk(ast.parse(ui_source))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for name in (
            "_can_choose_folder",
            "_refresh_pending_ui",
            "_set_scanning_ui",
            "_undo_last_move",
            "_continue_pending",
            "_abandon_pending",
            "_workbench_decide",
        ):
            self.assertNotIn(name, ui_defs, f"ui_qt_main 仍保留判定：{name}")
        self.assertIn("def can_choose_folder", controller_source)
        for intent in (
            "on_choose_folder_clicked",
            "on_start_clicked",
            "on_stop_clicked",
            "on_threshold_changed",
            "on_group_decided",
            "on_resume_pending",
            "on_abandon_pending",
            "on_undo_clicked",
            "on_workbench_closed",
            "on_close_requested",
        ):
            self.assertIn(f"def {intent}", controller_source)


if __name__ == "__main__":
    unittest.main()
