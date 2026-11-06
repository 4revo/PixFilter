"""PixFilter 纯逻辑扫描服务（第一步重构）。

硬约束：本模块**禁止依赖任何 GUI 框架 / 控件 / 事件循环 / 主线程轮询**，
因此纯逻辑可被任意界面层复用。

线程语义：
- 所有公开方法都可以在主线程调用；内部共享状态由 ``threading.RLock`` 保护。
- ``start_scan`` / ``undo_last_batch`` / ``undo_all_moves`` 会各自启动一个
  daemon 工作线程。
- 注入的回调（progress/log/group_ready/suspended/finished/undo_finished/error）
  可能来自工作线程，**UI 侧必须自行切回主线程**（Qt 版通过 queued signal）。
- ``apply_decision`` 是同步决策：扫描线程调用它会执行移动/跳过/挂起；
  挂起集合、批次栈（LIFO）与当前目录的唯一真相源都在本类。
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Optional

from pixfilter_core import (
    MISSING_KEEPER_ERROR,
    MoveResult,
    ScanReport,
    SuspendedGroup,
    TRASH_DIR_NAME,
    apply_review_decision,
    discard_group,
    group_members,
    pending_groups,
    pick_default_keeper,
    scan_folder,
)

ProgressCb = Callable[[str, int, int], None]
LogCb = Callable[[str, str], None]
GroupReadyCb = Callable[[object, int, int, Path], None]
GroupsReadyCb = Callable[[list, Path], None]
SuspendedCb = Callable[[SuspendedGroup], None]
FinishedCb = Callable[[ScanReport], None]
ReviewFinishedCb = Callable[[object], None]
# 撤销完成回调：items, restored, skipped, cleanup, failures(可选，失败原因分类)。
UndoFinishedCb = Callable[..., None]
ErrorCb = Callable[[str], None]

# 复核模式：逐组握手（旧路径）与一次性交批（工作台）互斥。
REVIEW_MODE_PER_GROUP = "per_group"
REVIEW_MODE_BATCH = "batch"
REVIEW_MODES = (REVIEW_MODE_PER_GROUP, REVIEW_MODE_BATCH)


def _noop(*_args, **_kwargs) -> None:
    return None


@dataclass
class DecisionResult:
    """一次 confirm/skip/suspend/discard_all 决策的执行结果。"""

    moves: list[MoveResult]
    suspended: bool = False
    duplicate: bool = False
    discarded: bool = False


@dataclass
class UndoBatch:
    """撤销栈条目：移动记录 + 所属组键（按组撤销时据此精确摘除）。"""

    items: list[tuple[Path, Path]]
    key: object = None


@dataclass
class UndoResult:
    """单组撤销的同步结果；ok=False 表示无批次或服务正忙。"""

    ok: bool
    restored: int = 0
    skipped: int = 0
    cleanup: str = ""
    error: str = ""


@dataclass
class ReviewSummary:
    """批处理复核的最终汇总（仅批处理模式产生）。"""

    total_groups: int = 0
    confirmed_groups: int = 0
    moved_files: int = 0
    skipped_groups: int = 0
    discarded_groups: int = 0
    suspended_groups: int = 0
    skipped_images: int = 0
    cancelled: bool = False

    def summary_text(self) -> str:
        text = (
            f"确认 {self.confirmed_groups} 组，移动 {self.moved_files} 张文件，"
            f"跳过 {self.skipped_groups} 组，挂起 {self.suspended_groups} 组"
        )
        if self.discarded_groups:
            text += f"，整组丢弃 {self.discarded_groups} 组"
        if self.skipped_images:
            text += f"，跳过 {self.skipped_images} 张无法读取的图片"
        if self.cancelled:
            text += "（已取消）"
        return text + "。"


class ScanService:
    """扫描编排、挂起组、批次/撤销栈与空回收站清理。"""

    VALID_STATES = ("idle", "scanning", "reviewing", "undoing")

    def __init__(
        self,
        *,
        progress_cb: Optional[ProgressCb] = None,
        log_cb: Optional[LogCb] = None,
        group_ready_cb: Optional[GroupReadyCb] = None,
        groups_ready_cb: Optional[GroupsReadyCb] = None,
        suspended_cb: Optional[SuspendedCb] = None,
        finished_cb: Optional[FinishedCb] = None,
        review_finished_cb: Optional[ReviewFinishedCb] = None,
        undo_finished_cb: Optional[UndoFinishedCb] = None,
        error_cb: Optional[ErrorCb] = None,
        review_mode: str = REVIEW_MODE_PER_GROUP,
    ) -> None:
        if review_mode not in REVIEW_MODES:
            raise ValueError(f"未知复核模式：{review_mode}")
        self._progress_cb = progress_cb or _noop
        self._log_cb = log_cb or _noop
        self._group_ready_cb = group_ready_cb or _noop
        self._groups_ready_cb = groups_ready_cb or _noop
        self._suspended_cb = suspended_cb or _noop
        self._finished_cb = finished_cb or _noop
        self._review_finished_cb = review_finished_cb or _noop
        self._undo_finished_cb = undo_finished_cb or _noop
        self._error_cb = error_cb or _noop
        self._review_mode = review_mode

        self._batch_groups: list = []
        self._batch_root: Optional[Path] = None
        self._batch_index: dict[int, int] = {}
        self._batch_actions: list[Optional[str]] = []
        self._batch_summary = ReviewSummary()
        self._batch_active = False

        self._lock = threading.RLock()
        self._state = "idle"
        self._cancel_event = threading.Event()
        self._decision_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._scan_thread: Optional[threading.Thread] = None
        self._undo_thread: Optional[threading.Thread] = None
        self._batches: list[list[tuple[Path, Path]]] = []
        self._pending: list[SuspendedGroup] = []
        self._pending_uid = 0
        self._folder: Optional[Path] = None
        self._last_report: Optional[ScanReport] = None
        self._progress: dict[str, tuple[int, int]] = {"md5": (0, 0), "phash": (0, 0)}
        self._progress_rank = 0

    # ---------- 只读状态 ----------
    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def folder(self) -> Optional[Path]:
        with self._lock:
            return self._folder

    @folder.setter
    def folder(self, value: Path | str | None) -> None:
        with self._lock:
            self._folder = Path(value).expanduser().resolve() if value is not None else None

    @property
    def last_report(self) -> Optional[ScanReport]:
        with self._lock:
            return self._last_report

    @property
    def pending_groups(self) -> tuple[SuspendedGroup, ...]:
        with self._lock:
            return tuple(self._pending)

    @property
    def undo_stack(self) -> list[list[tuple[Path, Path]]]:
        """只读快照；内部栈由 record_batch/undo 维护。"""
        with self._lock:
            return [list(entry.items) for entry in self._batches]

    @property
    def has_undo(self) -> bool:
        with self._lock:
            return bool(self._batches)

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    @property
    def review_mode(self) -> str:
        with self._lock:
            return self._review_mode

    def set_review_mode(self, mode: str) -> bool:
        """扫描/复核进行中禁止切换模式；两种模式互斥。"""
        if mode not in REVIEW_MODES:
            raise ValueError(f"未知复核模式：{mode}")
        with self._lock:
            if self._state != "idle":
                return False
            self._review_mode = mode
            return True

    def progress_snapshot(self) -> tuple[str, int, int]:
        """返回 (最近阶段, 已完成, 总数)，供 UI 在停止时展示。"""
        with self._lock:
            stage = "phash" if self._progress_rank >= 2 else "md5"
            done, total = self._progress.get(stage, (0, 0))
            return stage, done, total

    # ---------- 扫描 ----------
    def start_scan(
        self,
        folder: Path | str,
        threshold: int,
        max_workers: Optional[int] = None,
    ) -> bool:
        """启动扫描线程；忙或有挂起组时返回 False。"""
        with self._lock:
            if self._state != "idle" or self._pending:
                return False
            root = Path(folder).expanduser().resolve()
            if not root.is_dir():
                raise ValueError(f"目录不存在或不可访问：{root}")
            self._folder = root
            self._cancel_event = threading.Event()
            self._decision_queue = queue.Queue()
            self._last_report = None
            self._progress = {"md5": (0, 0), "phash": (0, 0)}
            self._progress_rank = 0
            self._reset_batch_tracking()
            self._state = "scanning"
            thread = threading.Thread(
                target=self._run_scan,
                args=(root, max(0, int(threshold)), max_workers),
                name="pixfilter-scan",
                daemon=True,
            )
            self._scan_thread = thread
        thread.start()
        return True

    def request_cancel(self) -> None:
        """请求停止；逐组模式补 skip 解除阻塞，批处理模式未确认组全部挂起。"""
        self._cancel_event.set()
        with self._lock:
            batch = self._batch_active
            reviewing = self._state == "reviewing"
        if batch:
            self._cancel_batch_review()
        elif reviewing:
            self._decision_queue.put(("skip", None))

    def submit_decision(self, action: str, keeper=None) -> None:
        """把 UI 的决策投递给正在等待的扫描线程（不执行文件操作）。"""
        self._decision_queue.put((action, keeper))

    def _run_scan(
        self,
        root: Path,
        threshold: int,
        max_workers: Optional[int],
    ) -> None:
        try:
            report = scan_folder(
                root,
                distance=threshold,
                log=self._safe_log,
                should_stop=self._cancel_event.is_set,
                cancel_event=self._cancel_event,
                progress_cb=self._emit_progress,
                max_workers=max_workers,
            )
            with self._lock:
                self._last_report = report

            groups = pending_groups(report)
            if report.cancelled or self._cancel_event.is_set():
                report.cancelled = True
                self._safe_log("扫描已停止，未处理任何待确认分组。", "warn")
            elif self._review_mode == REVIEW_MODE_BATCH and groups:
                # 一次性交批：工作线程不等待决策，直接结束。
                self._begin_batch_review(report, groups)
            else:
                self._review_groups(report, groups)

            if self._cancel_event.is_set() and not report.cancelled:
                report.cancelled = True
                self._safe_log("已停止：剩余待确认分组保持原样。", "warn")

            self._safe_log(report.summary_text(), "summary")
            self._safe(self._finished_cb, report)
        except Exception as exc:
            self._safe(self._error_cb, str(exc))
        finally:
            with self._lock:
                # 批处理模式下复核仍在进行，保持 reviewing 直到全部决策完成。
                self._state = "reviewing" if self._batch_active else "idle"

    # ---------- 批处理复核 ----------
    def _reset_batch_tracking(self) -> None:
        with self._lock:
            self._batch_groups = []
            self._batch_root = None
            self._batch_index = {}
            self._batch_actions = []
            self._batch_summary = ReviewSummary()
            self._batch_active = False

    def _begin_batch_review(self, report: ScanReport, groups: list) -> None:
        groups = list(groups)
        with self._lock:
            self._batch_groups = groups
            self._batch_root = report.root
            self._batch_index = {id(group): i for i, group in enumerate(groups)}
            self._batch_actions = [None] * len(groups)
            self._batch_summary = ReviewSummary(
                total_groups=len(groups),
                skipped_images=report.skipped_images,
            )
            self._batch_active = True
            self._state = "reviewing"
        self._safe_log(
            f"[待确认] 共 {len(groups)} 组，已一次性交批处理复核。", "group"
        )
        self._safe(self._groups_ready_cb, list(groups), report.root)

    def _reserve_batch_decision(
        self, group, action: str
    ) -> tuple[Optional[int], bool]:
        """预占一个批处理决策槽；重复决策返回 (index, True) 且不执行。"""
        with self._lock:
            if not (
                self._review_mode == REVIEW_MODE_BATCH and self._batch_active
            ):
                return None, False
            index = self._batch_index.get(id(group))
            if index is None:
                return None, False
            if self._batch_actions[index] is not None:
                return index, True
            self._batch_actions[index] = action
            return index, False

    def _complete_batch_decision(
        self,
        index: int,
        action: str,
        outcome: DecisionResult,
        group,
        root: Path,
    ) -> DecisionResult:
        register = False
        summary: Optional[ReviewSummary] = None
        total = 0
        with self._lock:
            total = len(self._batch_groups)
            if action == "confirm" and not outcome.suspended:
                self._batch_summary.confirmed_groups += 1
                self._batch_summary.moved_files += sum(
                    1 for result in outcome.moves if result.ok
                )
            elif action == "discard_all" and not outcome.suspended:
                self._batch_summary.discarded_groups += 1
                self._batch_summary.moved_files += sum(
                    1 for result in outcome.moves if result.ok
                )
            elif action == "skip":
                self._batch_summary.skipped_groups += 1
            else:
                self._batch_summary.suspended_groups += 1
                register = True
            report = self._last_report
            if report is not None:
                report.moves.extend(outcome.moves)
                if action == "skip":
                    report.skipped_groups += 1
                elif action == "discard_all" and not register:
                    report.discarded_groups += 1
                elif register:
                    report.suspended_count += 1
            if self._batch_active and all(
                value is not None for value in self._batch_actions
            ):
                self._batch_active = False
                self._state = "idle"
                summary = replace(self._batch_summary)
        if register:
            self._register_suspended(group, index + 1, total, root)
        if summary is not None:
            self._safe_log(
                f"[复核完成] {summary.summary_text()}", "summary"
            )
            self._safe(self._review_finished_cb, summary)
        return outcome

    def _cancel_batch_review(self) -> None:
        items: list[tuple[int, object]] = []
        summary: Optional[ReviewSummary] = None
        with self._lock:
            if not self._batch_active:
                return
            undecided = [
                i for i, value in enumerate(self._batch_actions) if value is None
            ]
            for index in undecided:
                self._batch_actions[index] = "suspend"
            items = [(index, self._batch_groups[index]) for index in undecided]
            total = len(self._batch_groups)
            root = self._batch_root
            self._batch_summary.cancelled = True
            self._batch_summary.suspended_groups += len(undecided)
            report = self._last_report
            if report is not None:
                report.cancelled = True
                report.suspended_count += len(undecided)
            self._batch_active = False
            self._state = "idle"
            summary = replace(self._batch_summary)
        for index, group in items:
            self._register_suspended(group, index + 1, total, root)
        self._safe_log(
            f"已停止批量复核：{len(items)} 组未确认，保持原样并转入“待处理重复组”。",
            "warn",
        )
        self._safe(self._review_finished_cb, summary)

    def _review_groups(self, report: ScanReport, groups: list) -> None:
        total = len(groups)
        for index, group in enumerate(groups, 1):
            if self._cancel_event.is_set():
                report.cancelled = True
                self._safe_log("已停止：剩余待确认分组保持原样。", "warn")
                return
            kind_text = "精确重复" if group.kind == "exact" else "视觉相似"
            self._safe_log(
                f"[待确认 {index}/{total}] {kind_text} · {group.total} 张", "group"
            )
            with self._lock:
                self._state = "reviewing"
            self._safe(self._group_ready_cb, group, index, total, report.root)

            action, keeper = self._decision_queue.get()
            if keeper is None and action != "discard_all":
                keeper = pick_default_keeper(group)
            with self._lock:
                self._state = "scanning"

            outcome = self.apply_decision(group, action, keeper, root=report.root)
            report.moves.extend(outcome.moves)

            if action == "confirm":
                if not keeper.path.is_file():
                    self._safe_log(
                        f"[缺失] {MISSING_KEEPER_ERROR}，本组未移动任何文件，"
                        f"已重新加入待处理：{keeper.rel_path}",
                        "error",
                    )
                    report.suspended_count += 1
                    self._register_suspended(group, index, total, report.root)
                elif outcome.suspended:
                    report.suspended_count += 1
                    self._register_suspended(group, index, total, report.root)
            elif action == "skip":
                report.skipped_groups += 1
            elif action == "discard_all":
                if outcome.suspended:
                    report.suspended_count += 1
                    self._register_suspended(group, index, total, report.root)
                else:
                    report.discarded_groups += 1
            else:
                report.suspended_count += 1
                paths = "、".join(str(item.rel_path) for item in group_members(group))
                self._safe_log(
                    f"[挂起] 第 {index}/{total} 组已保留在“待处理重复组”，"
                    f"未移动任何文件：{paths}",
                    "warn",
                )
                self._register_suspended(group, index, total, report.root)

    def _register_suspended(self, group, index: int, total: int, root: Path) -> None:
        item = self.add_suspended(group, index, total, root)
        self._safe(self._suspended_cb, item)

    # ---------- 决策与批次 ----------
    def apply_decision(
        self,
        group,
        action: str,
        keeper,
        *,
        root: Path | str | None = None,
    ) -> DecisionResult:
        """同步执行一次决策；不负责弹窗，也不负责待处理集合的增删。

        批处理模式下每组只生效一次：决策槽先到先得，重复提交直接返回
        ``duplicate=True`` 且不产生任何文件移动。
        """
        base = Path(root).expanduser().resolve() if root is not None else self._folder
        if base is None:
            raise ValueError("尚未设置扫描目录")
        # 整组丢弃没有 keeper：忽略调用方传入的保留项，全部成员进入移动队列。
        if action == "discard_all":
            keeper = None
        elif keeper is None:
            keeper = pick_default_keeper(group)

        batch_index, duplicate = self._reserve_batch_decision(group, action)
        if duplicate:
            total = len(self._batch_groups)
            self._safe_log(
                f"[重复决策] 第 {batch_index + 1}/{total} 组已处理，"
                f"忽略重复的 {action} 决策，未移动任何文件。",
                "warn",
            )
            return DecisionResult(moves=[], duplicate=True)

        try:
            if action == "confirm":
                if not keeper.path.is_file():
                    outcome = DecisionResult(moves=[], suspended=True)
                else:
                    results = apply_review_decision(
                        base, group, keeper, True, log=self._safe_log
                    )
                    batch = [
                        (result.source, result.destination)
                        for result in results
                        if result.ok and result.destination is not None
                    ]
                    self.record_batch(batch, key=id(group))
                    suspended = any(
                        not result.ok and result.error == MISSING_KEEPER_ERROR
                        for result in results
                    )
                    outcome = DecisionResult(moves=results, suspended=suspended)
            elif action == "discard_all":
                results = discard_group(base, group, log=self._safe_log)
                batch = [
                    (result.source, result.destination)
                    for result in results
                    if result.ok and result.destination is not None
                ]
                self.record_batch(batch, key=id(group))
                # 有空缺/移动失败时保留原文件并转挂起，避免“已处理”假象。
                suspended = any(not result.ok for result in results)
                outcome = DecisionResult(
                    moves=results, suspended=suspended, discarded=True
                )
            elif action == "skip":
                apply_review_decision(
                    base, group, keeper, False, log=self._safe_log
                )
                outcome = DecisionResult(moves=[])
            else:
                # suspend：不移动文件，只由调用方（批处理模式由服务）
                # 把组加入待处理集合。
                outcome = DecisionResult(moves=[], suspended=True)
        except Exception as exc:
            # 单个组的异常不能阻塞整批复核：转为挂起并保留原文件。
            self._safe_log(
                f"[错误] 第 {batch_index + 1 if batch_index is not None else 0} 组"
                f"决策执行失败，已按挂起处理：{type(exc).__name__}: {exc}",
                "error",
            )
            outcome = DecisionResult(moves=[], suspended=True)

        if batch_index is not None:
            self._complete_batch_decision(
                batch_index, action, outcome, group, base
            )
        return outcome

    def record_batch(
        self, batch: list[tuple[Path, Path]], key: object = None
    ) -> None:
        if not batch:
            return
        with self._lock:
            self._batches.append(UndoBatch(items=list(batch), key=key))

    # ---------- 待处理集合 ----------
    def add_suspended(
        self, group, index: int, total: int, root: Path | str
    ) -> SuspendedGroup:
        with self._lock:
            self._pending_uid += 1
            item = SuspendedGroup(
                group=group,
                index=index,
                total=total,
                root=Path(root),
                uid=self._pending_uid,
            )
            self._pending.append(item)
            return item

    def remove_suspended(self, item: SuspendedGroup) -> bool:
        with self._lock:
            try:
                self._pending.remove(item)
            except ValueError:
                return False
            return True

    # ---------- 撤销 ----------
    def undo_last_batch(self) -> bool:
        """启动 LIFO 撤销线程；无批次或正在忙时返回 False。"""
        with self._lock:
            if self._state != "idle" or not self._batches:
                return False
            entry = self._batches[-1]
            self._state = "undoing"
            thread = threading.Thread(
                target=self._run_undo,
                args=(entry, self._undo_finished_cb),
                name="pixfilter-undo",
                daemon=True,
            )
            self._undo_thread = thread
        thread.start()
        return True

    def undo_all_moves(self) -> bool:
        """启动“全量撤销”线程：按批次倒序恢复所有移动，然后复位批次状态。

        无批次或服务忙（扫描/撤销中）返回 False。完成后清空撤销栈、待处理组
        与复核决策槽，服务回到 idle，保证可以重新选择目录并再次扫描。
        """
        with self._lock:
            # 批处理复核（reviewing 且 batch_active）可被全量撤销终止；
            # 逐组复核正在等待决策时拒绝，避免扫描线程永久阻塞。
            if self._state not in ("idle", "reviewing") or not self._batches:
                return False
            if self._state == "reviewing" and not self._batch_active:
                return False
            # 提前终止复核会话，避免撤销期间还有“等待决策”的残留状态。
            self._batch_active = False
            entries = list(self._batches)
            self._state = "undoing"
            thread = threading.Thread(
                target=self._run_undo_all,
                args=(entries, self._undo_finished_cb),
                name="pixfilter-undo-all",
                daemon=True,
            )
            self._undo_thread = thread
        thread.start()
        return True

    def _run_undo_all(
        self,
        entries: list[UndoBatch],
        on_finished: Optional[UndoFinishedCb] = None,
    ) -> None:
        """LIFO 恢复全部批次；单文件失败只记录并跳过，绝不中断整体流程。"""
        items: list[tuple[Path, Path]] = []
        failures: list[str] = []
        restored = 0
        skipped = 0
        try:
            for entry in reversed(entries):
                batch_items = list(entry.items)
                items.extend(batch_items)
                count_ok, count_skip, reasons = self._restore_items_detailed(
                    batch_items
                )
                restored += count_ok
                skipped += count_skip
                failures.extend(reasons)
            cleanup = self.cleanup_empty_trash()
        except Exception as exc:
            # 兜底：即使出现未预期异常也必须完成状态复位。
            self._safe_log(
                f"[全量撤销] 未预期错误：{type(exc).__name__}: {exc}", "error"
            )
            failures.append(f"未预期错误（{type(exc).__name__}）")
            cleanup = "missing"
        with self._lock:
            self._batches.clear()
            self._pending.clear()
            self._reset_batch_tracking()
            self._last_report = None
            self._state = "idle"
        self._safe(
            on_finished or self._undo_finished_cb,
            items,
            restored,
            skipped,
            cleanup,
            failures,
        )

    def undo_group(
        self, group, on_finished: Optional[UndoFinishedCb] = None
    ) -> UndoResult:
        """同步撤销“某一个组”的移动批次，不影响其他组。

        按 ``id(group)`` 在栈中查找该组最近一个批次（通常恰好一个），
        撤销成功/失败后都按对象身份精确摘除该条目；即使它不在栈顶，
        其余批次的顺序也保持不变，之后的全量撤销仍按 LIFO 正常执行。
        """
        key = id(group)
        with self._lock:
            # 批处理复核在等待剩余组决策时状态为 reviewing，此时仍允许撤销
            # 已处理组；只有扫描/撤销进行中才拒绝。
            if self._state not in ("idle", "reviewing"):
                return UndoResult(ok=False, error="busy")
            entry = next(
                (item for item in reversed(self._batches) if item.key == key),
                None,
            )
            if entry is None:
                return UndoResult(ok=False, error="missing")
            self._state = "undoing"

        items = list(entry.items)
        restored, skipped = self._restore_items(items)
        cleanup = self.cleanup_empty_trash()
        with self._lock:
            # 按对象身份摘除，避免误删内容相同的其他批次。
            for index, candidate in enumerate(self._batches):
                if candidate is entry:
                    del self._batches[index]
                    break
            self._rollback_batch_entry(entry, restored)
            self._state = "reviewing" if self._batch_active else "idle"
        self._safe(on_finished or self._undo_finished_cb, items, restored, skipped, cleanup)
        return UndoResult(
            ok=True, restored=restored, skipped=skipped, cleanup=cleanup
        )

    def _run_undo(
        self, entry: UndoBatch, on_finished: Optional[UndoFinishedCb] = None
    ) -> None:
        items = list(entry.items)
        restored, skipped = self._restore_items(items)
        cleanup = self.cleanup_empty_trash()
        with self._lock:
            for index, candidate in enumerate(self._batches):
                if candidate is entry:
                    del self._batches[index]
                    break
            self._state = "idle"
        self._safe(on_finished or self._undo_finished_cb, items, restored, skipped, cleanup)

    def _rollback_batch_entry(self, entry: UndoBatch, restored: int) -> None:
        """单组撤销成功后回退该组的批处理统计，并允许它重新决策。

        只重置该组自己的决策槽与计数；其他组的槽位和待处理集合不受影响。
        """
        if entry.key is None:
            return
        index = self._batch_index.get(entry.key)
        if index is None or self._batch_actions[index] is None:
            return
        action = self._batch_actions[index]
        if action == "confirm":
            self._batch_summary.confirmed_groups = max(
                0, self._batch_summary.confirmed_groups - 1
            )
        elif action == "discard_all":
            self._batch_summary.discarded_groups = max(
                0, self._batch_summary.discarded_groups - 1
            )
        else:
            return
        self._batch_summary.moved_files = max(
            0, self._batch_summary.moved_files - max(0, restored)
        )
        # 清空该组的决策槽：之后它再次被决策时视为新决策，并重新入栈。
        self._batch_actions[index] = None
        report = self._last_report
        if report is None:
            return
        if action == "discard_all":
            report.discarded_groups = max(0, report.discarded_groups - 1)
        destinations = {destination for _source, destination in entry.items}
        if destinations:
            # 只移除这批已还原的移动记录，其他组的移动结果保持原样。
            report.moves = [
                move
                for move in report.moves
                if not (move.ok and move.destination in destinations)
            ]

    def _restore_items(self, items: list[tuple[Path, Path]]) -> tuple[int, int]:
        """把回收站中的文件搬回原路径；同名冲突不覆盖，保留回收站副本并跳过。"""
        restored, skipped, _reasons = self._restore_items_detailed(items)
        return restored, skipped

    def _restore_items_detailed(
        self, items: list[tuple[Path, Path]]
    ) -> tuple[int, int, list[str]]:
        """逐项尽力还原，返回 (成功数, 跳过数, 失败原因分类列表)。"""
        restored = 0
        skipped = 0
        reasons: list[str] = []
        for source, destination in items:
            if not destination.exists():
                skipped += 1
                reasons.append("回收站文件缺失")
                self._safe_log(
                    f"[撤销] 回收站中的文件已不存在，跳过：{destination}", "warn"
                )
                continue
            if source.exists():
                skipped += 1
                reasons.append("原位置同名未覆盖")
                self._safe_log(
                    f"[撤销] 原位置已存在同名文件，未覆盖：{source}"
                    f"（文件仍保留在回收站：{destination}）",
                    "warn",
                )
                continue
            try:
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(destination), str(source))
            except Exception as exc:
                skipped += 1
                reasons.append(f"恢复失败（{type(exc).__name__}）")
                self._safe_log(
                    f"[撤销] 恢复失败：{source}"
                    f"（{type(exc).__name__}: {exc}）",
                    "error",
                )
                continue
            restored += 1
            self._safe_log(f"已恢复：{source}", "info")
        return restored, skipped, reasons

    def cleanup_empty_trash(
        self,
        root: Path | str | None = None,
        trash: Path | str | None = None,
        log: Optional[LogCb] = None,
    ) -> str:
        """只对“当前目录/重复文件”根目录本身做 listdir + os.rmdir。"""
        root_value = root if root is not None else self._folder
        if root_value is None:
            return "missing"
        root_path = Path(root_value).expanduser()
        expected = root_path / TRASH_DIR_NAME
        target = Path(trash) if trash is not None else expected
        if (
            target.name != TRASH_DIR_NAME
            or target.parent != root_path
            or not target.is_dir()
        ):
            return "invalid"
        log_fn = log or self._safe_log
        try:
            if not os.listdir(target):
                os.rmdir(target)
                return "removed"
        except OSError:
            log_fn("无法清理空的重复文件夹（可能被占用），已跳过", "warn")
            return "error"
        return "kept"

    # ---------- 关闭 ----------
    def stop(self) -> None:
        """取消扫描并尝试 join 扫描/撤销线程（不阻塞过久）。"""
        self.request_cancel()
        for thread in (self._scan_thread, self._undo_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=0.5)

    # ---------- 回调安全网 ----------
    def _safe(self, callback: Callable[..., None], *args) -> None:
        try:
            callback(*args)
        except Exception:
            # 回调失败不能拖垮服务线程；UI 侧自行记录。
            pass

    def _safe_log(self, message: str, kind: str = "info") -> None:
        self._safe(self._log_cb, message, kind)

    def _emit_progress(self, stage: str, done: int, total: int) -> None:
        """只向外转发单调递增且不回退阶段的进度。"""
        normalized = "md5" if stage == "md5" else "phash"
        rank = 1 if normalized == "md5" else 2
        with self._lock:
            if rank < self._progress_rank:
                return
            self._progress_rank = max(self._progress_rank, rank)
            previous, _ = self._progress.get(normalized, (0, 0))
            if done <= previous:
                return
            self._progress[normalized] = (done, max(0, total))
        self._safe(self._progress_cb, normalized, done, total)
