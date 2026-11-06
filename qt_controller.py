"""PixFilter 的纯 Python 主控制器（View–Controller–Service 的控制器层）。

硬约束（由 ``tests/test_architecture.py`` 的 AST 扫描强制）：
- 本模块**不得** import 任何 Qt / PySide6 / PyQt / shiboken 模块，也不得出现
  任何 Qt 类型或控件名；
- 项目内依赖只有 ``config``、``scan_service``（及其转出的 pixfilter 纯数据），
  其余全部是标准库；
- 控制器不 import ``ui_qt_main``：视图通过依赖注入传入，控制器只调用
  ``MainViewPort`` 里声明的语义动作，因此依赖方向恒为
  ``ui_qt_main → qt_controller → scan_service``，无环。

线程语义：
- ``ScanService`` 的回调可能来自工作线程。视图用 ``post(name, *args)`` 把回调
  投递到本控制器的**线程安全队列**；Qt 主线程的定时器周期性调用 ``pump()``
  在事件循环里依次执行。工作线程因此从不触碰任何界面对象。
- 所有改状态的方法最终都在主线程（``pump`` 或视图直接调用）执行，因此控制器
  内部状态无需加锁；``queue.Queue`` 负责唯一的跨线程交接。

状态唯一来源：
- 服务状态（``state`` / ``pending_groups`` / ``has_undo``）与控制器状态
  （``_scanning`` / ``_pending_busy`` / ``_undo_busy`` / 待处理行 /
  工作台开关）合并后，通过端口推给视图；视图不私存任何业务副本。
"""

from __future__ import annotations

import queue
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, Protocol

from config import (
    ACCENT,
    AMBER,
    GITHUB_URL,
    GREEN,
    MUTED,
    RED,
    THRESHOLD_DEFAULT,
    THRESHOLD_MAX,
    THRESHOLD_MIN,
)
from scan_service import (
    TRASH_DIR_NAME,
    group_members,
    pick_default_keeper,
)


@dataclass(frozen=True)
class PendingRow:
    """待处理面板的一行；由控制器生成，视图只负责渲染。"""

    key: int
    label: str
    kind: str
    total: str
    summary: str
    status: str
    selected: bool = False


class MainViewPort(Protocol):
    """控制器需要的全部界面能力（只放语义动作，不含 Qt 类型/控件名）。

    每个方法都必须是“让视图做某件事”或“从视图读一个用户输入”，
    具体控件、样式、平台调用由视图实现。
    """

    def set_choose_folder_enabled(self, enabled: bool, reason_text: str) -> None:
        """选择目录按钮的可用状态；``reason_text`` 为空表示无需提示。"""

    def set_scan_buttons(
        self, scanning: bool, can_stop: bool, can_start: bool
    ) -> None:
        """扫描按钮组：``scanning`` 控制进度/滑块，``can_stop``/``can_start`` 控制按钮。"""

    def set_threshold(self, value: int) -> None:
        """把阈值控件与文案同步到控制器校验后的值。"""

    def get_threshold(self) -> int:
        """读取阈值控件当前值（视图侧已限制在合法区间）。"""

    def append_log(self, level: str, text: str) -> None:
        """追加一条日志；``level`` 决定颜色/加粗，由视图映射样式。"""

    def append_copyable_path(self, path: Path) -> None:
        """追加可点击复制的“当前扫描目录”日志行。"""

    def set_status(self, text: str, color: str) -> None:
        """设置状态栏文案与颜色（颜色常量来自 config）。"""

    def clear_log(self) -> None:
        """清空日志区域。"""

    def show_progress(self, stage: str, done: int, total: int) -> None:
        """展示两阶段进度（视图负责把 stage 映射到进度条区间）。"""

    def hide_progress(self) -> None:
        """隐藏进度条。"""

    def refresh_pending(self, rows: list[PendingRow]) -> None:
        """按控制器给的快照刷新待处理面板（含空态与行选中标记）。"""

    def set_pending_buttons(
        self,
        continue_enabled: bool,
        abandon_enabled: bool,
        abandon_all_enabled: bool,
    ) -> None:
        """待处理面板的“继续处理/放弃处理选中组/全部放弃”按钮状态。"""

    def confirm_abandon_all(self) -> bool:
        """弹出「全部放弃」二次确认；返回 True 表示用户点了“确定”。

        这是一个可注入的视图能力：测试替换本方法即可自动应答，
        不需要真实模态框，也不会阻塞 offscreen 事件循环。
        """

    def set_undo_enabled(self, enabled: bool) -> None:
        """撤销按钮状态。"""

    def set_github_available(self) -> None:
        """GitHub 入口可用（视图据此打开按钮，不做平台判断）。"""

    def show_workbench(
        self, groups: list, root: Path, initial_index: int = 0
    ) -> None:
        """打开批量复核工作台并聚焦 ``initial_index``；创建失败时抛异常。"""

    def focus_workbench(self, index: int) -> None:
        """复用已打开的工作台，切到指定组并置前（不创建第二个窗口）。"""

    def close_workbench(self) -> None:
        """关闭工作台且不重复决策（服务已负责挂起未确认组）。"""

    def is_workbench_open(self) -> bool:
        """工作台当前是否可见/存在。"""

    def show_toast(self, text: str) -> None:
        """顶部非阻塞 Toast。"""

    def show_error(self, text: str) -> None:
        """错误提示（非阻塞优先）。"""

    def show_info(self, title: str, text: str) -> None:
        """信息对话框（保留原有标题与文案）。"""

    def show_warning(self, title: str, text: str) -> None:
        """警告对话框（保留原有标题与文案）。"""

    def select_folder(self, start_dir: Path) -> Optional[Path]:
        """弹出目录选择框，返回用户选中的目录（取消返回 None）。"""

    def open_folder(self, path: Path) -> None:
        """用系统文件管理器打开目录；失败时抛异常，由控制器组织文案。"""

    def open_github_url(self) -> None:
        """用默认浏览器打开项目主页；失败时抛异常，由控制器组织文案。"""


class ScanController:
    """扫描编排与界面状态的唯一决策者（不含任何 Qt 依赖）。"""

    def __init__(self, service, view: MainViewPort) -> None:
        self._service = service
        self._view = view

        self._queue: "queue.Queue[tuple[str, tuple]]" = queue.Queue()
        self._closed = False
        self._closing = False
        self._scanning = False
        self._pending_busy = False
        self._undo_busy = False
        self._scan_completed = False
        self._workbench_open = False
        self._workbench_group_count = 0
        self._workbench_root: Optional[Path] = None
        self._workbench_from_pending = False
        self._workbench_groups: list = []
        self._group_roots: dict[int, Path] = {}
        self._pending_status: dict[int, str] = {}
        self._selected: list[int] = []
        self._last_summary = None
        self._threshold = int(THRESHOLD_DEFAULT)

    # ---------- 跨线程投递 ----------
    def post(self, name: str, *args) -> None:
        """工作线程把服务回调投递到控制器；由主线程 ``pump`` 执行。"""
        if self._closed:
            return
        self._queue.put((name, args))

    def pump(self) -> None:
        """在主线程排空回调队列；视图定时器只负责调用本方法。"""
        while True:
            try:
                name, args = self._queue.get_nowait()
            except queue.Empty:
                return
            if self._closed:
                return
            handler = getattr(self, name, None)
            if handler is None:
                continue
            try:
                handler(*args)
            except Exception as exc:  # 回调异常不能拖垮事件循环
                self._view.append_log(
                    "error", f"控制器内部错误：{type(exc).__name__}: {exc}"
                )

    def start(self) -> None:
        """视图就绪后推一次初始状态。"""
        self._view.set_threshold(self._threshold)
        self._view.set_github_available()
        self._refresh()

    # ---------- 只读状态（供视图/测试查询，不参与决策） ----------
    @property
    def scanning(self) -> bool:
        return self._scanning

    @property
    def pending_busy(self) -> bool:
        return self._pending_busy

    @property
    def undo_busy(self) -> bool:
        return self._undo_busy

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def workbench_open(self) -> bool:
        return self._workbench_open

    @property
    def threshold(self) -> int:
        return self._threshold

    def can_choose_folder(self) -> bool:
        """选择目录的单一判定：空闲、无待处理组、无工作台、无撤销。"""
        return (
            not self._closed
            and not self._scanning
            and not self._pending_busy
            and not self._undo_busy
            and self._service.state == "idle"
            and not self._service.pending_groups
            and not self._pending_status
            and not self._workbench_open
        )

    # ---------- 推送给视图 ----------
    def _rows(self) -> list[PendingRow]:
        pending = list(self._service.pending_groups)
        present = {item.uid for item in pending}
        for key in list(self._pending_status):
            if key not in present:
                del self._pending_status[key]
        self._selected = [key for key in self._selected if key in present]
        if pending and not self._selected:
            self._selected = [pending[0].uid]
        rows: list[PendingRow] = []
        for item in pending:
            group = item.group
            first = group_members(group)[0]
            parent = str(first.rel_path.parent)
            if parent in ("", "."):
                parent = "根目录"
            rows.append(
                PendingRow(
                    key=item.uid,
                    label=f"{item.index}/{item.total}",
                    kind="完全重复" if group.kind == "exact" else "相似",
                    total=str(group.total),
                    summary=f"{first.path.name} · {parent}",
                    status=self._pending_status.get(item.uid, "待处理"),
                    selected=item.uid in self._selected,
                )
            )
        return rows

    def _refresh(self) -> None:
        if self._closed:
            return
        self._view.refresh_pending(self._rows())
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        if self._closed:
            return
        pending = self._service.pending_groups
        has = bool(pending)
        busy = (
            self._scanning
            or self._pending_busy
            or self._undo_busy
            or self._workbench_open
        )
        self._view.set_choose_folder_enabled(
            self.can_choose_folder(), self._choose_reason()
        )
        self._view.set_scan_buttons(
            self._scanning, self._scanning, not busy and not has
        )
        selected = bool(self._selected)
        self._view.set_pending_buttons(
            not busy and has,
            not busy and has and selected,
            not busy and has,
        )
        self._view.set_undo_enabled(self._undo_enabled())
        if has and not busy:
            self._view.set_status(
                f"还有 {len(pending)} 组待处理 · "
                "处理完或放弃后才能重新选择目录",
                AMBER,
            )

    def _undo_enabled(self) -> bool:
        return (
            self._service.has_undo
            and not self._scanning
            and not self._pending_busy
            and not self._undo_busy
            and not self._workbench_open
            and not self._closed
        )

    def _choose_reason(self) -> str:
        pending = len(self._service.pending_groups) or len(self._pending_status)
        if self.can_choose_folder():
            return ""
        if pending:
            return f"还有 {pending} 组待处理，处理完或放弃后才能重新选择目录"
        if self._workbench_open:
            return "批量复核进行中，完成或全部挂起后才能重新选择目录"
        if self._undo_busy:
            return "撤销进行中，请稍候"
        if self._scanning or self._service.state != "idle":
            return "扫描进行中，请稍候"
        return ""

    # ---------- 用户动作：目录 / 阈值 ----------
    def on_choose_folder_clicked(self) -> None:
        if self._closed:
            return
        if not self.can_choose_folder():
            # 禁用态兜底：即使被程序化调用也不弹对话框、不改状态。
            return
        start = self._service.folder or Path.home()
        selected = self._view.select_folder(start)
        if not selected:
            return
        self._service.folder = Path(selected)
        self._view.append_copyable_path(Path(selected))
        self._view.set_status("就绪 · 点击“开始扫描”", MUTED)

    def on_threshold_changed(self, value: int) -> None:
        if self._closed:
            return
        value = max(THRESHOLD_MIN, min(THRESHOLD_MAX, int(value)))
        self._threshold = value
        self._view.set_threshold(value)

    def get_threshold(self) -> int:
        return self._threshold

    # ---------- 用户动作：扫描 ----------
    def on_start_clicked(self) -> None:
        if self._closed:
            return
        if self._workbench_open:
            return
        if self._scanning or self._pending_busy or self._undo_busy:
            return
        if self._service.pending_groups:
            self._view.show_info(
                "还有待处理组",
                f"当前还有 {len(self._service.pending_groups)} 组被 X 挂起。\n"
                "请先在“待处理重复组”面板继续处理或放弃处理，再重新扫描。",
            )
            return
        folder = self._service.folder
        if folder is None:
            self._view.show_info("尚未选择文件夹", "请先选择要扫描的文件夹。")
            return
        if not folder.is_dir():
            self._view.show_warning(
                "目录不可用", f"文件夹不存在或不可访问：\n{folder}"
            )
            return

        distance = self.get_threshold()
        self._scanning = True
        self._scan_completed = False
        self._pending_busy = False
        self._view.set_scan_buttons(True, False, False)
        self._view.set_status("正在扫描…", MUTED)
        self._view.clear_log()
        self._view.append_copyable_path(folder)
        try:
            started = self._service.start_scan(folder, distance)
        except ValueError as exc:
            self._scanning = False
            self._refresh()
            self._view.set_status("扫描失败 · 详情见日志", RED)
            self._view.append_log("error", f"扫描失败：{exc}")
            return
        if not started:
            self._scanning = False
            self._refresh()
            self._view.set_status("无法开始扫描 · 服务当前不空闲", AMBER)
            return
        self._view.set_scan_buttons(True, True, False)
        self._sync_buttons()

    def on_stop_clicked(self) -> None:
        if self._closed:
            return
        if not self._scanning and not self._workbench_open:
            return
        # 批处理模式：服务会把未确认组挂起；工作台只做收尾，不重复决策。
        self._service.request_cancel()
        if self._workbench_open:
            self._workbench_open = False
            try:
                self._view.close_workbench()
            except Exception:
                pass
        self._view.set_scan_buttons(True, False, False)
        self._view.set_status("正在停止…", AMBER)
        self._view.append_log(
            "warn",
            "已请求停止：不再弹出新的确认组；当前已弹出的组仍按你的选择处理。",
        )
        self._refresh()

    # ---------- 服务回调（经队列到主线程） ----------
    def on_log(self, message: str, kind: str = "info") -> None:
        if self._closed:
            return
        self._view.append_log(kind, message)

    def on_progress(self, stage: str, done: int, total: int) -> None:
        if self._closed or not self._scanning:
            return
        normalized = "md5" if stage == "md5" else "phash"
        done = max(0, int(done))
        total = max(0, int(total))
        self._view.show_progress(normalized, done, total)
        label = "MD5" if normalized == "md5" else "pHash"
        self._view.set_status(f"{label} 进度 {done}/{total}", MUTED)

    def on_group_ready(self, group, index: int, total: int, root) -> None:
        """逐组模式回调；主界面已统一走批量工作台，这里只做安全兜底。"""
        if self._closed:
            return
        if self._service.cancel_event.is_set():
            self._service.submit_decision("skip", None)
            self._view.append_log(
                "warn", "已停止：跳过尚未复核的确认组，未移动文件。"
            )
            return
        # 批处理模式下服务不会发出该回调；若被逐组模式触发，则挂起而非
        # 打开单组弹窗，保证文件零移动且稍后可从待处理列表继续。
        self._service.submit_decision("suspend", None)
        self._view.append_log(
            "warn",
            "单组对比弹窗已移除；该组转为待处理，"
            "可在“待处理重复组”双击打开批量复核工作台。",
        )

    def on_suspended(self, suspended) -> None:
        if self._closed:
            return
        self._pending_status[suspended.uid] = "待处理"
        self._refresh()

    def on_groups_ready(self, groups, root) -> None:
        if self._closed or not groups or self._workbench_open:
            return
        prepared = list(groups)
        try:
            self._view.show_workbench(prepared, Path(root), 0)
        except Exception as exc:
            self._view.append_log(
                "error",
                "批量复核工作台创建失败："
                f"{type(exc).__name__}: {exc}；本批未移动任何文件。",
            )
            return
        self._workbench_open = True
        self._workbench_group_count = len(groups)
        self._workbench_root = Path(root)
        self._workbench_from_pending = False
        self._workbench_groups = list(prepared)
        self._group_roots = {id(group): Path(root) for group in prepared}
        self._view.set_status(f"批量复核 · 共 {len(groups)} 组", ACCENT)
        self._view.append_log(
            "group",
            f"[批量复核] 已一次性载入 {len(groups)} 组，可在左侧任意切换。",
        )
        self._refresh()

    def on_group_decided(self, group, action: str, keeper) -> object:
        """工作台决策入口：按来源目录执行，并同步待处理表格与按钮。"""
        root = self._group_roots.get(id(group), self._workbench_root)
        if root is not None:
            outcome = self._service.apply_decision(
                group, action, keeper, root=root
            )
        else:
            outcome = self._service.apply_decision(group, action, keeper)

        items = [
            item
            for item in self._service.pending_groups
            if self._group_key(item.group) == self._group_key(group)
        ]
        if items:
            item = items[0]
            suspended = bool(getattr(outcome, "suspended", False))
            if action == "skip" or (
                action in ("confirm", "discard_all") and not suspended
            ):
                self._remove_suspended(item)
            else:
                self._set_pending_status(item, "待处理")
            if not self._service.pending_groups:
                self._view.set_status(
                    "待处理组已全部处理 · 关闭工作台后可重新扫描", ACCENT
                )
            elif action in ("confirm", "skip", "discard_all"):
                self._view.set_status(
                    f"批量复核 · 剩余 {len(self._service.pending_groups)} 组待处理",
                    ACCENT,
                )
        return outcome

    def on_group_undo(self, group) -> object:
        """工作台“撤销本组”：只还原当前组，不影响其他批次与 LIFO 顺序。"""
        if self._closed or self._scanning or self._undo_busy or self._pending_busy:
            return None
        result = self._service.undo_group(group)
        if result is not None and getattr(result, "ok", False):
            self._restore_pending_entry(group)
        return result

    def _restore_pending_entry(self, group) -> None:
        """撤销本组后把该组放回待处理表（已在表中则不重复添加）。"""
        key = self._group_key(group)
        if any(
            self._group_key(item.group) == key
            for item in self._service.pending_groups
        ):
            return
        root = self._group_roots.get(id(group), self._workbench_root)
        if root is None:
            return
        total = len(self._workbench_groups) or 1
        index = (
            self._workbench_groups.index(group) + 1
            if group in self._workbench_groups
            else 0
        )
        item = self._service.add_suspended(group, index, total, root)
        self._pending_status[item.uid] = "待处理"
        self._refresh()

    def on_workbench_closed(self) -> None:
        was_pending = self._workbench_from_pending
        self._workbench_open = False
        self._workbench_group_count = 0
        self._workbench_root = None
        self._workbench_from_pending = False
        self._workbench_groups = []
        self._group_roots.clear()
        if self._closed:
            return
        if was_pending and not self._service.pending_groups:
            self._pending_cleared()
        else:
            self._refresh()

    def on_review_finished(self, summary) -> None:
        if self._closed:
            return
        self._last_summary = summary
        self._refresh()
        if getattr(summary, "cancelled", False):
            self._view.set_status(
                f"已停止 · 确认 {summary.confirmed_groups} 组"
                f" · 挂起 {summary.suspended_groups} 组",
                AMBER,
            )
        else:
            status = (
                f"扫描完成 · 确认 {summary.confirmed_groups} 组"
                f" · 移入“{TRASH_DIR_NAME}” {summary.moved_files} 个"
                f" · 跳过 {summary.skipped_groups} 组"
            )
            discarded_groups = getattr(summary, "discarded_groups", 0)
            if discarded_groups:
                status += f" · 整组丢弃 {discarded_groups} 组"
            if summary.suspended_groups:
                status += f" · 挂起 {summary.suspended_groups} 组"
            elif summary.skipped_images:
                status += f" · 跳过 {summary.skipped_images} 张无法读取"
            self._view.set_status(
                status,
                AMBER
                if summary.suspended_groups or summary.skipped_images
                else GREEN,
            )
            self._view.show_toast(
                f"扫描完成，确认 {summary.confirmed_groups} 组，"
                f"移动 {summary.moved_files} 张"
            )
        self._view.append_log("summary", f"[批量复核] {summary.summary_text()}")

    def on_finished(self, report) -> None:
        if self._closed:
            return
        self._scanning = False
        self._scan_completed = not report.cancelled
        self._refresh()
        pending = len(self._service.pending_groups)

        if self._workbench_open and not report.cancelled:
            # 批处理模式：扫描线程已交批结束，复核仍由工作台继续。
            self._view.append_log(
                "group",
                "扫描阶段完成：待确认组已一次性交给批量复核工作台。",
            )
            self._view.set_status(
                f"扫描完成 · 等待批量复核（{self._workbench_group_count} 组）",
                ACCENT,
            )
            self._refresh()
            return

        if report.cancelled:
            _stage, done, total = self._service.progress_snapshot()
            if total <= 0:
                total = report.scanned
            status = (
                f"已停止（已处理 {done}/{total}）"
                f" · 已移入“{TRASH_DIR_NAME}” {report.moved_count} 个"
            )
            if report.skipped_images:
                status += f" · 跳过 {report.skipped_images} 张无法读取"
            if pending:
                status += f" · 还有 {pending} 组待处理"
                self._view.append_log(
                    "warn",
                    f"扫描已停止：还有 {pending} 组已挂起，"
                    "可在“待处理重复组”继续处理或放弃。",
                )
            self._view.set_status(status, AMBER)
            self._refresh()
            return

        status = f"扫描完成 · 移入“{TRASH_DIR_NAME}” {report.moved_count} 个"
        if report.skipped_groups:
            status += f" · 跳过 {report.skipped_groups} 组"
        if report.skipped_images:
            status += f" · 跳过 {report.skipped_images} 张无法读取"
        if report.failed_moves:
            status += f" · 失败 {len(report.failed_moves)} 个"
        if pending:
            status += f" · 还有 {pending} 组待处理"
        self._view.set_status(
            status,
            AMBER
            if (report.failed_moves or pending or report.skipped_images)
            else GREEN,
        )
        if pending:
            self._view.append_log(
                "summary",
                f"扫描完成：还有 {pending} 组已挂起，"
                "可在“待处理重复组”继续处理或放弃。",
            )
            self._view.show_toast(f"扫描完成，还有 {pending} 组待处理")
        elif report.skipped_images:
            self._view.show_toast(
                f"扫描完成，跳过 {report.skipped_images} 张无法读取"
            )
        else:
            self._view.show_toast("扫描完成！")
        self._refresh()

    def on_error(self, message: str) -> None:
        if self._closed:
            return
        self._scanning = False
        self._refresh()
        self._view.set_status("扫描失败 · 详情见日志", RED)
        self._view.append_log("error", f"扫描失败：{message}")

    # ---------- 待处理面板动作 ----------
    def on_pending_selection_changed(self, keys) -> None:
        if self._closed:
            return
        valid = {item.uid for item in self._service.pending_groups}
        self._selected = [int(key) for key in keys if int(key) in valid]
        self._sync_buttons()

    def on_resume_pending(self, key: Optional[int] = None) -> None:
        """打开（或复用）批量复核工作台；``key`` 指定初始焦点组。"""
        if self._closed or self._scanning or self._pending_busy:
            return
        pending = sorted(self._service.pending_groups, key=lambda item: item.index)
        if not pending:
            self._view.set_status("请先在待处理列表选择一组", MUTED)
            return

        if key is not None:
            focus_uid = int(key)
        elif self._selected:
            focus_uid = self._selected[0]
        else:
            focus_uid = pending[0].uid
        items = pending

        if self._workbench_open:
            self._focus_existing(items, focus_uid)
            return

        prepared = [
            self._prepare_group(item.group, item.root) for item in items
        ]
        roots = {
            id(group): item.root for group, item in zip(prepared, items)
        }
        initial = next(
            (i for i, item in enumerate(items) if item.uid == focus_uid), 0
        )
        try:
            self._view.show_workbench(prepared, items[0].root, initial)
        except Exception as exc:
            self._view.append_log(
                "error",
                "批量复核工作台创建失败："
                f"{type(exc).__name__}: {exc}；未移动任何文件。",
            )
            return

        self._workbench_open = True
        self._workbench_group_count = len(prepared)
        self._workbench_root = items[0].root
        self._workbench_from_pending = True
        self._workbench_groups = list(prepared)
        self._group_roots = roots
        for item in items:
            self._pending_status[item.uid] = "处理中"
        self._view.set_status(
            f"批量复核 · 待处理 {len(prepared)} 组 · 可在左侧切换", ACCENT
        )
        self._view.append_log(
            "group",
            f"[继续处理] 已载入 {len(prepared)} 组挂起到批量复核工作台。",
        )
        self._refresh()

    def _focus_existing(self, items: list, focus_uid: int) -> None:
        """工作台已打开时只切换焦点，不创建第二个实例。"""
        target = next(
            (item for item in items if item.uid == focus_uid), None
        )
        if target is None:
            return
        key = self._group_key(target.group)
        for index, group in enumerate(self._workbench_groups):
            if self._group_key(group) == key:
                self._view.focus_workbench(index)
                return
        self._view.set_status(
            "该组已在当前工作台之外，请关闭工作台后再继续", AMBER
        )

    def _group_key(self, group) -> tuple:
        """组的内容标识：成员路径集合，用于跨对象匹配挂起组。"""
        return tuple(sorted(str(member.path) for member in group_members(group)))

    def _prepare_group(self, group, root: Path):
        """把挂起组适配成工作台可渲染的组结构。

        成员可能已被其他组移走：这里只在“默认保留项”缺失时改用仍然存在
        的成员作为默认 keeper；缺失成员本身保留在组内，由工作台显示占位。
        """
        members = group_members(group)
        if not members:
            return group
        existing = [member for member in members if member.path.is_file()]
        if not existing or len(existing) == len(members):
            return group
        default = pick_default_keeper(group)
        if default.path.is_file():
            keeper = default
        else:
            keeper = pick_default_keeper(
                replace(
                    group, keeper=existing[0], duplicates=existing[1:]
                )
            )
        duplicates = [member for member in members if member is not keeper]
        if not duplicates:
            return group
        return replace(group, keeper=keeper, duplicates=duplicates)

    def on_abandon_pending(self) -> None:
        if self._closed or self._scanning or self._pending_busy:
            return
        items = [item for item in self._service.pending_groups if item.uid in self._selected]
        if not items:
            self._view.set_status("请先在待处理列表选择一组", MUTED)
            return
        for item in items:
            paths = "、".join(
                str(member.rel_path) for member in group_members(item.group)
            )
            self._view.append_log(
                "warn",
                f"[放弃处理] 用户主动放弃第 {item.index}/{item.total} 组，"
                f"未移动任何文件：{paths}",
            )
            self._remove_suspended(item)
        if self._service.pending_groups:
            self._view.set_status(
                f"还有 {len(self._service.pending_groups)} 组待处理 · "
                "可继续处理或放弃",
                AMBER,
            )
        else:
            self._pending_cleared()

    def on_abandon_all_pending(self) -> None:
        """放弃全部待处理组（含挂起组）：零移动，二次确认后整体清空。"""
        if (
            self._closed
            or self._scanning
            or self._pending_busy
            or self._undo_busy
            or self._workbench_open
        ):
            return
        pending = sorted(
            self._service.pending_groups, key=lambda item: item.index
        )
        if not pending:
            return
        if not bool(self._view.confirm_abandon_all()):
            # 用户取消：不刷新、不改状态、不写日志，界面保持原样。
            return

        total = len(pending)
        paths: list[str] = []
        for item in pending:
            paths.extend(
                str(member.rel_path) for member in group_members(item.group)
            )
            self._remove_suspended(item)

        self._selected = []
        self._pending_status.clear()
        self._view.append_log(
            "warn",
            f"[全部放弃] 用户主动放弃全部 {total} 组（含挂起组），"
            f"未移动任何文件：{'、'.join(paths)}",
        )
        self._refresh()
        self._view.set_status("已全部放弃", AMBER)

    def _set_pending_status(self, suspended, status: str) -> None:
        if suspended is None:
            return
        self._pending_status[suspended.uid] = status
        self._refresh()

    def _remove_suspended(self, suspended) -> None:
        if suspended is None:
            return
        self._pending_status.pop(suspended.uid, None)
        self._selected = [key for key in self._selected if key != suspended.uid]
        self._service.remove_suspended(suspended)
        self._refresh()

    def _pending_cleared(self) -> None:
        report = self._service.last_report
        status = "扫描完成 · 待处理组已全部处理"
        if report is not None:
            status += f" · 移入“{TRASH_DIR_NAME}” {report.moved_count} 个"
            if report.skipped_images:
                status += f" · 跳过 {report.skipped_images} 张无法读取"
        self._view.set_status(status + " · 可重新扫描", GREEN)
        self._view.append_log(
            "summary", "待处理重复组已全部处理完，可重新扫描。"
        )
        self._refresh()
        if self._scan_completed:
            self._view.show_toast("扫描完成！")

    # ---------- 撤销 ----------
    def on_undo_clicked(self) -> None:
        if (
            self._closed
            or self._undo_busy
            or self._scanning
            or self._pending_busy
            or not self._service.has_undo
        ):
            return
        total = sum(len(batch) for batch in self._service.undo_stack)
        if not self._service.undo_all_moves():
            return
        self._undo_busy = True
        self._refresh()
        self._view.set_status(
            f"正在全量撤销所有移动（{total} 个文件）…", ACCENT
        )
        self._view.append_log(
            "info",
            f"开始全量撤销：共 {total} 个文件，正在恢复…",
        )

    def on_undo_finished(
        self,
        batch,
        restored: int,
        skipped: int,
        cleanup: str = "missing",
        failures=None,
    ) -> None:
        if self._closed:
            return
        self._undo_busy = False
        # 全量撤销会终止复核会话，并清空所有待处理/挂起组。
        self._pending_status.clear()
        self._selected = []
        if self._workbench_open:
            self._workbench_open = False
            self._workbench_group_count = 0
            self._workbench_root = None
            self._workbench_from_pending = False
            self._workbench_groups = []
            self._group_roots.clear()
            try:
                self._view.close_workbench()
            except Exception:
                pass
        self._refresh()
        failure_text = self._undo_failures_text(failures, skipped)
        failed = max(int(skipped or 0), sum(Counter(failures or []).values()))
        if failure_text:
            self._view.set_status(
                f"全量撤销完成 · 恢复 {restored} 个，失败 {failed} 个", AMBER
            )
            if restored:
                self._view.append_log(
                    "summary", "✅ 已全量撤销所有移动，所有图片已复原。"
                )
            self._view.append_log("warn", failure_text)
        elif restored:
            status = "已全量撤销所有移动"
            if cleanup == "removed":
                status += " · 已清理空的重复文件夹"
            self._view.set_status(status, GREEN)
            self._view.append_log(
                "summary", "✅ 已全量撤销所有移动，所有图片已复原。"
            )
        else:
            self._view.set_status("全量撤销完成 · 没有文件被恢复", AMBER)
            self._view.append_log(
                "warn",
                "全量撤销完成：没有文件被恢复（冲突或缺失，详见上方警告）。",
            )

    def _undo_failures_text(self, failures, skipped: int) -> str:
        """把失败原因分类聚合成一条警告汇总；无失败时返回空串。"""
        counts = Counter(failures or [])
        failed = max(int(skipped or 0), sum(counts.values()))
        if not failed:
            return ""
        parts = [
            f"{reason} {count} 个" for reason, count in counts.most_common()
        ]
        if not parts:
            parts = [f"原因未知 {failed} 个"]
        return f"⚠️ 有 {failed} 个文件未能还原：{'、'.join(parts)}（详见上方警告）"

    # ---------- 平台动作 ----------
    def on_open_trash_clicked(self) -> None:
        if self._closed:
            return
        folder = self._service.folder
        target = folder / TRASH_DIR_NAME if folder is not None else None
        if target is None or not target.is_dir():
            message = "还没有产生重复文件"
            if target is not None:
                message += f"（目录不存在：{target}）"
            self._view.set_status(message, MUTED)
            self._view.append_log("warn", message)
            return
        try:
            self._view.open_folder(target)
        except Exception as exc:
            message = f"无法打开文件夹，请手动访问 {target}"
            detail = (
                f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            )
            self._view.set_status(f"{message}（{detail}）", AMBER)
            self._view.append_log("warn", f"⚠️ {message}（{detail}）")
        else:
            self._view.append_log("info", f"已打开重复文件目录：{target}")

    def on_github_clicked(self) -> None:
        if self._closed:
            return
        try:
            self._view.open_github_url()
        except Exception as exc:
            message = f"无法打开浏览器，请手动访问 {GITHUB_URL}"
            detail = (
                f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            )
            self._view.set_status(f"{message}（{detail}）", AMBER)
            self._view.append_log("warn", f"⚠️ {message}（{detail}）")

    # ---------- 关闭 ----------
    def on_close_requested(self) -> None:
        """关闭收尾：先取消服务，再关闭工作台，最后由视图停定时器。"""
        if self._closing:
            return
        self._closing = True
        self._closed = True
        self._workbench_open = False
        self._workbench_groups = []
        self._group_roots.clear()
        self._drain()
        # 1) 取消服务：停止扫描线程、挂起未确认组（回调因 closed 被丢弃）。
        self._service.stop()
        # 2) 再关闭视图窗口（discard 语义：不重复决策）。
        try:
            self._view.close_workbench()
        except Exception:
            pass

    def _drain(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return


__all__ = [
    "MainViewPort",
    "PendingRow",
    "ScanController",
]
