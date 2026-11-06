"""控制器纯单测：不导入 Qt、不需要显示器。

用 FakeService + FakeView 记录控制器推给视图的指令序列，覆盖：
- 选择目录按钮的状态机（空闲/待处理/工作台/扫描/撤销）；
- 扫描 → groups_ready → 工作台 → review_finished 的状态与 Toast 文案；
- 挂起 → 继续/放弃的流程与按钮刷新；
- 撤销的前置条件与结果文案；
- 关闭顺序（先取消服务，再关工作台/弹窗）；
- 跨线程投递（工作线程 post，主线程 pump）。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import config  # noqa: E402
from pixfilter_core import (  # noqa: E402
    DuplicateGroup,
    ImageInfo,
    ScanReport,
    SuspendedGroup,
    TRASH_DIR_NAME,
)
from qt_controller import MainViewPort, PendingRow, ScanController  # noqa: E402


class FakeService:
    """只实现控制器用到的那部分服务接口，记录调用顺序。"""

    def __init__(self, folder: Path | None = None) -> None:
        self.state = "idle"
        self.folder = folder
        self.pending_groups: list[SuspendedGroup] = []
        self.has_undo = False
        self.undo_stack: list[list] = []
        self.last_report = None
        self.cancel_event = threading.Event()
        self.calls: list[tuple] = []
        self.start_result = True
        self.report = None
        self._progress = ("md5", 0, 0)
        self._uid = 0
        self.undo_group_result = SimpleNamespace(
            ok=True, restored=1, skipped=0, cleanup="removed", error=""
        )

    # ---- 控制器调用的 API ----
    def start_scan(self, folder, distance, max_workers=None):
        self.calls.append(("start_scan", Path(folder), distance))
        if not self.start_result:
            return False
        self.state = "scanning"
        return True

    def request_cancel(self):
        self.calls.append(("request_cancel",))
        self.cancel_event.set()

    def submit_decision(self, action, keeper):
        self.calls.append(("submit_decision", action, keeper))

    def apply_decision(self, group, action, keeper=None, root=None):
        self.calls.append(("apply_decision", action, root))
        return SimpleNamespace(
            ok=True, outcome=action, suspended=(action == "suspend")
        )

    def undo_last_batch(self):
        self.calls.append(("undo_last_batch",))
        if self.undo_stack:
            self.undo_stack.pop()
        self.has_undo = bool(self.undo_stack)
        return True

    def undo_all_moves(self):
        self.calls.append(("undo_all_moves",))
        self.undo_stack = []
        self.has_undo = False
        self.pending_groups = []
        self.state = "idle"
        return True

    def undo_group(self, group):
        self.calls.append(("undo_group", id(group)))
        return self.undo_group_result

    def remove_suspended(self, suspended):
        self.calls.append(("remove_suspended", suspended.uid))
        if suspended in self.pending_groups:
            self.pending_groups.remove(suspended)

    def stop(self):
        self.calls.append(("stop",))
        self.state = "idle"

    def progress_snapshot(self):
        return self._progress

    # ---- 测试辅助 ----
    def add_suspended(self, group, index: int, total: int, root: Path) -> SuspendedGroup:
        self._uid += 1
        suspended = SuspendedGroup(group=group, index=index, total=total, root=root, uid=self._uid)
        self.pending_groups.append(suspended)
        return suspended


class FakeView:
    """实现 MainViewPort；把所有指令写进 calls 便于断言顺序与文案。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.choose_enabled = True
        self.choose_reason = ""
        self.scan_buttons = (False, False, True)
        self.threshold = config.THRESHOLD_DEFAULT
        self.status = ""
        self.status_color = ""
        self.rows: list[PendingRow] = []
        self.continue_enabled = False
        self.abandon_enabled = False
        self.abandon_all_enabled = False
        self.confirm_abandon_all_result = True
        self.confirm_abandon_all_calls = 0
        self.undo_enabled = False
        self.workbench_groups = None
        self.workbench_open = False
        self.workbench_error: Exception | None = None
        self.workbench_initial_index = 0
        self.workbench_focus_calls: list[int] = []
        self.toasts: list[str] = []
        self.folder_to_select: Path | None = None
        self.folder_error: Exception | None = None
        self.github_error: Exception | None = None
        self.messages: list[tuple[str, str]] = []
        self.progress: list[tuple] = []

    # ---- 端口实现 ----
    def set_choose_folder_enabled(self, enabled, reason_text):
        self.choose_enabled = enabled
        self.choose_reason = reason_text
        self.calls.append(("choose", enabled, reason_text))

    def set_scan_buttons(self, scanning, can_stop, can_start):
        self.scan_buttons = (scanning, can_stop, can_start)
        self.calls.append(("scan_buttons", scanning, can_stop, can_start))

    def set_threshold(self, value):
        self.threshold = value
        self.calls.append(("threshold", value))

    def get_threshold(self):
        return self.threshold

    def append_log(self, level, text):
        self.calls.append(("log", level, text))

    def append_copyable_path(self, path):
        self.calls.append(("path", Path(path)))

    def set_status(self, text, color):
        self.status = text
        self.status_color = color
        self.calls.append(("status", text, color))

    def clear_log(self):
        self.calls.append(("clear_log",))

    def show_progress(self, stage, done, total):
        self.progress.append((stage, done, total))
        self.calls.append(("progress", stage, done, total))

    def hide_progress(self):
        self.calls.append(("hide_progress",))

    def refresh_pending(self, rows):
        self.rows = list(rows)
        self.calls.append(("refresh", [row.key for row in rows]))

    def set_pending_buttons(
        self, continue_enabled, abandon_enabled, abandon_all_enabled
    ):
        self.continue_enabled = continue_enabled
        self.abandon_enabled = abandon_enabled
        self.abandon_all_enabled = abandon_all_enabled
        self.calls.append(
            (
                "pending_buttons",
                continue_enabled,
                abandon_enabled,
                abandon_all_enabled,
            )
        )

    def confirm_abandon_all(self):
        self.confirm_abandon_all_calls += 1
        self.calls.append(("confirm_abandon_all",))
        return self.confirm_abandon_all_result

    def set_undo_enabled(self, enabled):
        self.undo_enabled = enabled
        self.calls.append(("undo", enabled))

    def set_github_available(self):
        self.calls.append(("github",))

    def show_workbench(self, groups, root, initial_index=0):
        if self.workbench_error is not None:
            raise self.workbench_error
        self.workbench_groups = list(groups)
        self.workbench_open = True
        self.workbench_initial_index = int(initial_index)
        self.calls.append(("workbench", len(groups), Path(root), initial_index))

    def focus_workbench(self, index):
        self.workbench_focus_calls.append(int(index))
        self.calls.append(("focus_workbench", int(index)))

    def close_workbench(self):
        self.workbench_open = False
        self.calls.append(("close_workbench",))

    def is_workbench_open(self):
        return self.workbench_open

    def show_toast(self, text):
        self.toasts.append(text)
        self.calls.append(("toast", text))

    def show_error(self, text):
        self.messages.append(("error", text))

    def show_info(self, title, text):
        self.messages.append((title, text))

    def show_warning(self, title, text):
        self.messages.append((title, text))

    def select_folder(self, start_dir):
        self.calls.append(("select_folder", Path(start_dir)))
        return self.folder_to_select

    def open_folder(self, path):
        if self.folder_error is not None:
            raise self.folder_error

    def open_github_url(self):
        if self.github_error is not None:
            raise self.github_error


def make_group(base: Path, names: list[str]) -> DuplicateGroup:
    infos = []
    for name in names:
        path = base / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"pixfilter")
        infos.append(
            ImageInfo(
                path=path,
                rel_path=Path(name),
                file_size=9,
                width=8,
                height=8,
            )
        )
    return DuplicateGroup(kind="exact", keeper=infos[0], duplicates=infos[1:])


def make_report(cancelled: bool = False, moved: int = 0, skipped_images: int = 0) -> ScanReport:
    report = ScanReport(root=Path("."))
    report.cancelled = cancelled
    report.scanned = 5
    report.skipped_images = skipped_images
    for index in range(moved):
        report.moves.append(
            SimpleNamespace(ok=True, source=Path(f"s{index}"), destination=Path(f"d{index}"))
        )
    return report


class ControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="pixfilter-ctrl-"))
        self.service = FakeService(folder=self.base)
        self.view = FakeView()
        self.controller = ScanController(self.service, self.view)
        self.controller.start()

    def tearDown(self) -> None:
        shutil.rmtree(self.base, ignore_errors=True)

    # ---------- 端口与初始状态 ----------
    def test_a_port_is_fully_implemented(self) -> None:
        names = [name for name in dir(MainViewPort) if not name.startswith("_")]
        self.assertTrue(names)
        for name in names:
            self.assertTrue(
                hasattr(self.view, name), f"FakeView 缺少端口方法：{name}"
            )
        for name in (
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
            self.assertTrue(
                hasattr(self.controller, name), f"控制器缺少意图方法：{name}"
            )

    def test_b_initial_state(self) -> None:
        self.assertTrue(self.view.choose_enabled)
        self.assertEqual(self.view.choose_reason, "")
        self.assertEqual(self.view.scan_buttons, (False, False, True))
        self.assertFalse(self.view.undo_enabled)
        self.assertFalse(self.view.continue_enabled)
        self.assertEqual(self.controller.get_threshold(), config.THRESHOLD_DEFAULT)
        self.assertNotEqual(self.controller.threshold, 0)

    # ---------- 选择目录按钮状态机 ----------
    def test_c_choose_folder_state_machine(self) -> None:
        # 空闲 → 可用
        self.assertTrue(self.controller.can_choose_folder())

        # 有待处理组 → 禁用 + 数量提示
        group = make_group(self.base, ["a.png", "b.png"])
        self.controller.on_suspended(self.service.add_suspended(group, 1, 1, self.base))
        self.assertFalse(self.controller.can_choose_folder())
        self.assertIn("1 组待处理", self.view.choose_reason)
        self.assertFalse(self.view.choose_enabled)
        self.assertIn("待处理", self.view.status)

        # 清空 → 恢复
        self.controller.on_abandon_pending()
        self.assertTrue(self.controller.can_choose_folder())

        # 工作台打开 → 禁用 + 复核提示
        self.controller.on_groups_ready([group], self.base)
        self.assertFalse(self.controller.can_choose_folder())
        self.assertIn("复核", self.view.choose_reason)
        self.controller.on_workbench_closed()
        self.assertTrue(self.controller.can_choose_folder())

        # 扫描中 → 禁用
        self.controller.on_start_clicked()
        self.assertFalse(self.controller.can_choose_folder())
        self.assertIn("扫描", self.view.choose_reason)
        self.service.state = "idle"  # 真实服务在 finished 回调前已回到 idle
        self.controller.on_finished(make_report())
        self.assertTrue(self.controller.can_choose_folder())

        # 撤销中 → 禁用
        self.service.has_undo = True
        self.service.undo_stack = [["batch"]]
        self.controller.on_undo_clicked()
        self.assertFalse(self.controller.can_choose_folder())
        self.assertIn("撤销", self.view.choose_reason)
        self.controller.on_undo_finished(["batch"], 0, 0, "missing")
        self.assertTrue(self.controller.can_choose_folder())

    def test_d_choose_disabled_has_no_side_effect(self) -> None:
        group = make_group(self.base, ["a.png", "b.png"])
        self.controller.on_suspended(self.service.add_suspended(group, 1, 1, self.base))
        before = list(self.view.calls)
        self.view.folder_to_select = self.base
        self.controller.on_choose_folder_clicked()
        self.assertEqual(self.view.calls, before)
        self.assertFalse(any(call[0] == "select_folder" for call in self.view.calls))

    # ---------- 扫描 → 工作台 → 收尾 ----------
    def test_e_scan_workbench_review_finished(self) -> None:
        self.controller.on_start_clicked()
        self.assertTrue(self.controller.scanning)
        self.assertEqual(self.view.status, "正在扫描…")
        self.assertEqual(self.view.scan_buttons, (True, True, False))
        self.assertTrue(self.service.calls[-1][0] == "start_scan")
        self.assertEqual(self.service.calls[-1][2], config.THRESHOLD_DEFAULT)

        group = make_group(self.base, ["a.png", "b.png"])
        self.controller.on_groups_ready([group], self.base)
        self.assertIn(("workbench", 1, self.base, 0), self.view.calls)
        self.assertEqual(self.view.status, "批量复核 · 共 1 组")
        self.assertFalse(self.view.choose_enabled)

        self.controller.on_finished(make_report(moved=1))
        self.assertEqual(
            self.view.status, "扫描完成 · 等待批量复核（1 组）"
        )

        summary = SimpleNamespace(
            cancelled=False,
            confirmed_groups=1,
            suspended_groups=0,
            skipped_groups=0,
            skipped_images=0,
            moved_files=1,
            summary_text=lambda: "共扫描 5 张",
        )
        self.controller.on_review_finished(summary)
        self.assertIn("扫描完成", self.view.status)
        self.assertIn(TRASH_DIR_NAME, self.view.status)
        self.assertEqual(
            self.view.toasts[-1], "扫描完成，确认 1 组，移动 1 张"
        )

    def test_f_finish_toast_for_pending_and_skipped(self) -> None:
        group = make_group(self.base, ["a.png", "b.png"])
        self.controller.on_start_clicked()
        self.controller.on_suspended(self.service.add_suspended(group, 1, 1, self.base))
        self.controller.on_finished(make_report())
        self.assertEqual(self.view.toasts[-1], "扫描完成，还有 1 组待处理")
        self.assertIn("还有 1 组待处理", self.view.status)

        service = FakeService(folder=self.base)
        view = FakeView()
        controller = ScanController(service, view)
        controller.start()
        controller.on_start_clicked()
        controller.on_finished(make_report(skipped_images=2))
        self.assertEqual(view.toasts[-1], "扫描完成，跳过 2 张无法读取")

    def test_g_workbench_failure_is_logged_and_degraded(self) -> None:
        self.view.workbench_error = RuntimeError("boom")
        self.controller.on_start_clicked()
        self.controller.on_groups_ready([make_group(self.base, ["a.png", "b.png"])], self.base)
        self.assertFalse(self.controller.workbench_open)
        logs = [call for call in self.view.calls if call[0] == "log"]
        self.assertTrue(logs)
        self.assertIn("批量复核工作台创建失败", logs[-1][2])

    def test_h_cancelled_group_is_skipped(self) -> None:
        self.service.cancel_event.set()
        self.controller.on_group_ready(
            make_group(self.base, ["a.png", "b.png"]), 1, 1, self.base
        )
        self.assertIn(("submit_decision", "skip", None), self.service.calls)
        self.assertIn("跳过尚未复核的确认组", self.view.calls[-1][2])

    # ---------- 挂起 → 继续/放弃 ----------
    def test_i_resume_confirm_and_suspend(self) -> None:
        group = make_group(self.base, ["a.png", "b.png"])
        suspended = self.service.add_suspended(group, 1, 1, self.base)
        self.controller.on_suspended(suspended)
        self.assertEqual(len(self.view.rows), 1)
        self.assertTrue(self.view.rows[0].selected)
        self.assertTrue(self.controller.can_choose_folder() is False)

        self.controller.on_resume_pending()
        self.assertTrue(self.controller.workbench_open)
        self.assertEqual(len(self.view.workbench_groups), 1)
        self.assertEqual(self.view.workbench_initial_index, 0)

        self.controller.on_group_decided(group, "confirm", group.keeper)
        self.assertIn(("apply_decision", "confirm", self.base), self.service.calls)
        self.assertEqual(self.view.rows, [])
        self.assertIn("可重新扫描", self.view.status)

        # 关闭工作台后目录按钮恢复。
        self.controller.on_workbench_closed()
        self.assertTrue(self.controller.can_choose_folder())

        # 再挂起一组：工作台按 X（suspend）→ 行保留、状态回“待处理”。
        second = self.service.add_suspended(
            make_group(self.base, ["c.png", "d.png"]), 2, 2, self.base
        )
        self.controller.on_suspended(second)
        self.controller.on_resume_pending()
        self.controller.on_group_decided(second.group, "suspend", None)
        self.assertEqual(len(self.view.rows), 1)
        self.assertEqual(self.view.rows[0].status, "待处理")
        self.assertIn("可在左侧切换", self.view.status)

    def test_j_abandon_clears_and_toasts_only_after_scan(self) -> None:
        group = make_group(self.base, ["a.png", "b.png"])
        self.controller.on_suspended(self.service.add_suspended(group, 1, 1, self.base))
        self.controller.on_abandon_pending()
        self.assertEqual(self.view.rows, [])
        self.assertIn(("remove_suspended", 1), self.service.calls)
        self.assertIn("可重新扫描", self.view.status)
        self.assertEqual(self.view.toasts, [])

        self.controller._scan_completed = True
        self.controller.on_suspended(self.service.add_suspended(group, 1, 1, self.base))
        self.controller.on_abandon_pending()
        self.assertEqual(self.view.toasts[-1], "扫描完成！")

    def test_k_resume_without_pending_prompts(self) -> None:
        self.controller.on_pending_selection_changed([])
        self.controller.on_resume_pending()
        self.assertEqual(self.view.status, "请先在待处理列表选择一组")
        self.assertFalse(self.view.workbench_open)

        # 有挂起组但清空选择 → 打开工作台并含全部组。
        group = make_group(self.base, ["a.png", "b.png"])
        self.controller.on_suspended(self.service.add_suspended(group, 1, 1, self.base))
        self.controller.on_pending_selection_changed([])
        self.controller.on_resume_pending()
        self.assertTrue(self.view.workbench_open)
        self.assertEqual(len(self.view.workbench_groups), 1)

    # ---------- 撤销 ----------
    def test_l_undo_preconditions_and_result(self) -> None:
        self.service.has_undo = False
        self.controller.on_undo_clicked()
        self.assertNotIn(("undo_all_moves",), self.service.calls)

        self.service.has_undo = True
        self.service.undo_stack = [["batch"]]
        self.controller.on_undo_clicked()
        self.assertTrue(self.controller.undo_busy)
        self.assertIn("正在全量撤销所有移动", self.view.status)
        self.assertIn(("undo_all_moves",), self.service.calls)

        self.controller.on_undo_finished(["batch"], 2, 1, "kept", ["原位置同名未覆盖"])
        self.assertFalse(self.controller.undo_busy)
        self.assertIn("恢复 2 个", self.view.status)
        self.assertEqual(self.view.status_color, config.AMBER)

    def test_m_undo_all_restored_text(self) -> None:
        self.service.has_undo = True
        self.service.undo_stack = [["batch"]]
        self.controller.on_undo_clicked()
        self.controller.on_undo_finished(["batch"], 3, 0, "removed")
        self.assertEqual(
            self.view.status, "已全量撤销所有移动 · 已清理空的重复文件夹"
        )
        self.assertIn(
            ("log", "summary", "✅ 已全量撤销所有移动，所有图片已复原。"),
            self.view.calls,
        )

    def test_m1_undo_all_clears_pending_and_restores_choose(self) -> None:
        group = make_group(self.base, ["a.png", "b.png"])
        self.controller.on_suspended(
            self.service.add_suspended(group, 1, 1, self.base)
        )
        self.assertEqual(len(self.view.rows), 1)
        self.assertFalse(self.controller.can_choose_folder())

        self.service.has_undo = True
        self.service.undo_stack = [["batch-a"], ["batch-b"]]
        self.controller.on_undo_clicked()
        self.assertEqual(self.view.status, "正在全量撤销所有移动（2 个文件）…")
        self.controller.on_undo_finished([], 3, 0, "removed", [])

        self.assertEqual(self.view.rows, [])
        self.assertTrue(self.controller.can_choose_folder())
        self.assertTrue(self.view.choose_enabled)
        self.assertEqual(self.service.state, "idle")
        self.assertFalse(self.view.undo_enabled)
        self.assertEqual(self.view.status_color, config.GREEN)

    def test_m2_undo_all_partial_failure_is_warned(self) -> None:
        self.service.has_undo = True
        self.service.undo_stack = [["batch"]]
        self.controller.on_undo_clicked()
        self.controller.on_undo_finished(
            [],
            2,
            2,
            "kept",
            ["回收站文件缺失", "原位置同名未覆盖", "回收站文件缺失"],
        )
        self.assertFalse(self.controller.undo_busy)
        self.assertEqual(self.view.status_color, config.AMBER)
        self.assertIn("失败 3 个", self.view.status)
        warning = [c for c in self.view.calls if c[0] == "log" and c[1] == "warn"]
        self.assertTrue(warning)
        self.assertIn("回收站文件缺失 2 个", warning[-1][2])
        self.assertIn("原位置同名未覆盖 1 个", warning[-1][2])

    # ---------- 关闭 ----------
    def test_n_close_order_service_first(self) -> None:
        self.controller.on_groups_ready(
            [make_group(self.base, ["a.png", "b.png"])], self.base
        )
        self.controller.on_workbench_closed()
        group = make_group(self.base, ["c.png", "d.png"])
        self.controller.on_suspended(self.service.add_suspended(group, 1, 1, self.base))
        self.controller.on_resume_pending()
        self.assertTrue(self.controller.workbench_open)

        self.controller.on_close_requested()
        self.assertTrue(self.controller.closed)
        self.assertEqual(self.service.calls[-1], ("stop",))
        sequence = [
            call[0]
            for call in self.view.calls
            if call[0] == "close_workbench"
        ]
        self.assertEqual(sequence, ["close_workbench"])

        # 关闭后任何动作都不再推视图。
        before = len(self.view.calls)
        self.controller.on_start_clicked()
        self.controller.on_undo_clicked()
        self.assertEqual(len(self.view.calls), before)

    # ---------- 线程与投递 ----------
    def test_o_worker_callbacks_are_queued_until_pump(self) -> None:
        caught: list[str] = []

        def worker() -> None:
            self.controller.post("on_log", "来自工作线程", "warn")

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        logged = [call for call in self.view.calls if call[0] == "log"]
        self.assertEqual(logged, [])
        self.controller.pump()
        logged = [call for call in self.view.calls if call[0] == "log"]
        self.assertEqual(logged[-1], ("log", "warn", "来自工作线程"))

    def test_p_github_and_trash_errors_use_config_url(self) -> None:
        self.view.github_error = RuntimeError("no browser")
        self.controller.on_github_clicked()
        self.assertIn("无法打开浏览器", self.view.status)
        self.assertIn(config.GITHUB_URL, self.view.status)

        self.service.folder = None
        self.controller.on_open_trash_clicked()
        self.assertIn("还没有产生重复文件", self.view.status)

    def test_q_threshold_is_clamped(self) -> None:
        self.controller.on_threshold_changed(0)
        self.assertEqual(self.controller.get_threshold(), config.THRESHOLD_MIN)
        self.assertEqual(self.view.threshold, config.THRESHOLD_MIN)
        self.controller.on_threshold_changed(999)
        self.assertEqual(self.controller.get_threshold(), config.THRESHOLD_MAX)

    # r) 整组丢弃：待处理列表移除、无 keeper 不报错、状态更新
    def test_r_discard_all_clears_pending_without_keeper(self) -> None:
        group = make_group(self.base, ["a.png", "b.png"])
        suspended = self.service.add_suspended(group, 1, 1, self.base)
        self.controller.on_suspended(suspended)
        self.controller.on_resume_pending()
        self.assertTrue(self.controller.workbench_open)

        before = len(self.service.calls)
        outcome = self.controller.on_group_decided(group, "discard_all", None)
        self.assertIsNotNone(outcome)
        self.assertIn(
            ("apply_decision", "discard_all", self.base),
            self.service.calls[before:],
        )
        self.assertEqual(self.view.rows, [])
        self.assertEqual(self.service.pending_groups, [])
        self.assertIn("可重新扫描", self.view.status)
        self.assertIn(("remove_suspended", suspended.uid), self.service.calls)

    # s) 撤销本组：控制器把该组放回待处理表，且不重复添加
    def test_s_group_undo_restores_pending_entry(self) -> None:
        group = make_group(self.base, ["a.png", "b.png"])
        suspended = self.service.add_suspended(group, 1, 1, self.base)
        self.controller.on_suspended(suspended)
        self.controller.on_resume_pending()
        self.controller.on_group_decided(group, "confirm", group.keeper)
        self.assertEqual(self.service.pending_groups, [])

        result = self.controller.on_group_undo(group)
        self.assertTrue(result.ok)
        self.assertIn(("undo_group", id(group)), self.service.calls)
        self.assertEqual(len(self.service.pending_groups), 1)
        self.assertEqual(len(self.view.rows), 1)
        self.assertEqual(self.view.rows[0].status, "待处理")

        # 已在待处理表中：再次撤销本组不重复添加行。
        before = len(self.service.pending_groups)
        self.controller.on_group_undo(group)
        self.assertEqual(len(self.service.pending_groups), before)

        # 撤销失败（批次已不在栈中）不回填待处理表。
        self.controller.on_group_decided(group, "confirm", group.keeper)
        self.service.undo_group_result = SimpleNamespace(
            ok=False, restored=0, skipped=0, cleanup="", error="missing"
        )
        failed = self.controller.on_group_undo(group)
        self.assertFalse(failed.ok)
        self.assertEqual(self.service.pending_groups, [])

        # 扫描中禁止撤销本组：直接返回 None，不调用服务。
        service = FakeService(folder=self.base)
        view = FakeView()
        controller = ScanController(service, view)
        controller.start()
        controller.on_start_clicked()
        self.assertIsNone(controller.on_group_undo(group))
        self.assertFalse(any(call[0] == "undo_group" for call in service.calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
