"""Qt 对比弹窗的子进程用例。

由 ``tests/test_review_qt.py`` 以独立进程调度，避免 Qt 事件循环污染
主测试进程。每个用例把结果以单行 JSON 打到 stdout。

用法：python tests/qt_review_cases.py <case>
"""

from __future__ import annotations

import gc
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def _enable_offscreen() -> None:
    # 强制 offscreen（不是 setdefault）：调用方无法把子进程拉回真实桌面。
    os.environ["QT_QPA_PLATFORM"] = "offscreen"


def _show_offscreen(widget) -> None:
    """离屏显示：不创建可见窗口，也不占用桌面/焦点。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    assert QGuiApplication.platformName() == "offscreen", (
        QGuiApplication.platformName()
    )
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False))


def _make_image(path: Path, size=(64, 48), color=(200, 30, 30)) -> Path:
    from PIL import Image

    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def _info(path: Path, rel: str | None = None) -> "object":
    from pixfilter_core import ImageInfo
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
    return ImageInfo(
        path=path,
        rel_path=Path(rel or path.name),
        file_size=path.stat().st_size,
        width=width,
        height=height,
    )


def _group(kind: str, keeper, *duplicates):
    from pixfilter_core import DuplicateGroup

    return DuplicateGroup(kind=kind, keeper=keeper, duplicates=list(duplicates))


def case_a_import_offscreen() -> dict:
    _enable_offscreen()
    # 导入模块本身不得把旧 GUI 框架拉进来（仅作禁止出现断言）。
    before = "tkinter" in sys.modules
    import review_qt

    app = review_qt.ensure_qapplication()
    after = "tkinter" in sys.modules

    return {
        "ok": True,
        "platform": os.environ.get("QT_QPA_PLATFORM"),
        "qapp_ok": app is not None,
        "tkinter_before": before,
        "tkinter_after_import": after,
    }


def case_b_cards_and_pixmaps() -> dict:
    _enable_offscreen()
    import review_qt
    from PySide6.QtCore import QSize

    app = review_qt.ensure_qapplication()
    root = Path(tempfile.mkdtemp(prefix="pixfilter_qt_cards_"))
    try:
        # 完全重复组：默认选中路径排序第一（a.png）。
        first = _make_image(root / "a.png", (800, 600), (30, 120, 200))
        second = _make_image(root / "b.png", (400, 300), (30, 120, 200))
        third = _make_image(root / "c.png", (200, 150), (30, 120, 200))
        exact = _group(
            "exact",
            _info(first, "a.png"),
            _info(second, "b.png"),
            _info(third, "c.png"),
        )
        dialog = review_qt.QtReviewDialog(exact, 1, 2)
        self_checks = {
            "card_count": len(dialog.members),
            "thumbnail_count": len(dialog._thumbnail_labels),
            "placeholder_count": len(dialog._placeholders),
            "default_path": dialog.default.path.name,
            "selected_path": Path(dialog._selected_path()).name,
        }
        sizes = []
        for label in dialog._thumbnail_labels:
            pixmap = label.pixmap()
            self_checks.setdefault("pixmap_sizes", []).append(
                [pixmap.width(), pixmap.height()]
            )
            sizes.append(label.size())
        self_checks["label_sizes"] = [[s.width(), s.height()] for s in sizes]
        self_checks["max_bound"] = [
            size.width() <= 300 and size.height() <= 300 for size in sizes
        ]
        pixmaps = [
            label.pixmap() for label in dialog._thumbnail_labels
        ]
        self_checks["aspect_ok"] = all(
            abs(pix.width() / max(pix.height(), 1) - 4.0 / 3.0) < 0.02
            for pix in pixmaps
        )

        # 相似组：默认选中分辨率最高的一张。
        big = _make_image(root / "big.png", (300, 300), (200, 30, 30))
        small = _make_image(root / "small.png", (100, 100), (200, 30, 30))
        similar = _group("similar", _info(small, "small.png"), _info(big, "big.png"))
        similar_dialog = review_qt.QtReviewDialog(similar, 2, 2)
        similar_default = similar_dialog.default.path.name

        # GC 之后仍可渲染：强引用 + grab() 非空。
        _show_offscreen(dialog)
        gc.collect()
        still_renderable = all(
            label.pixmap() is not None and not label.pixmap().isNull()
            for label in dialog._thumbnail_labels
        )
        grabbed = dialog.grab()
        grabbed_ok = not grabbed.isNull()
        dialog._finish("confirm")
        similar_dialog._finish("confirm")

        return {
            "ok": True,
            "exact": self_checks,
            "similar_default": similar_default,
            "gc_still_renderable": still_renderable,
            "grab_ok": grabbed_ok,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_c_missing_placeholder() -> dict:
    _enable_offscreen()
    import review_qt

    review_qt.ensure_qapplication()
    root = Path(tempfile.mkdtemp(prefix="pixfilter_qt_missing_"))
    logs: list[tuple[str, str]] = []
    dialog = None
    try:
        good = _make_image(root / "good.png", (64, 48), (30, 200, 60))
        missing = root / "missing.png"
        from pixfilter_core import ImageInfo

        missing_info = ImageInfo(
            path=missing,
            rel_path=Path("missing.png"),
            file_size=0,
            width=0,
            height=0,
        )
        group = _group("exact", _info(good, "good.png"), missing_info)
        dialog = review_qt.QtReviewDialog(
            group, 1, 1, log=lambda message, kind="info": logs.append((kind, message))
        )
        joined = "\n".join(message for _kind, message in logs)
        return {
            "ok": True,
            "placeholder_count": len(dialog._placeholders),
            "thumbnail_count": len(dialog._thumbnail_labels),
            "log_has_path": str(missing) in joined,
            "log_has_exception": "FileNotFoundError" in joined,
            "single_failure_keeps_other": len(dialog._thumbnail_labels) == 1,
            "placeholder_text": (
                dialog._placeholders[0].text() if dialog._placeholders else ""
            ),
        }
    finally:
        if dialog is not None:
            dialog._finish("skip")
        shutil.rmtree(root, ignore_errors=True)


def case_d_decision_mapping() -> dict:
    _enable_offscreen()
    import review_qt
    from scan_service import ScanService
    from pixfilter_core import TRASH_DIR_NAME

    review_qt.ensure_qapplication()
    root = Path(tempfile.mkdtemp(prefix="pixfilter_qt_decide_"))
    try:
        service = ScanService()
        service.folder = root

        # 1) confirm：真实移动。
        first = _make_image(root / "confirm_a.png", (64, 48), (20, 120, 220))
        second = _make_image(root / "confirm_b.png", (64, 48), (20, 120, 220))
        shutil.copyfile(first, second)
        group = _group(
            "exact",
            _info(first, "confirm_a.png"),
            _info(second, "confirm_b.png"),
        )
        dialog = review_qt.QtReviewDialog(group, 1, 1)
        dialog._finish("confirm")
        action, keeper = dialog.decision
        outcome = service.apply_decision(group, action, keeper)
        trash = root / TRASH_DIR_NAME
        confirm_moves = len(outcome.moves)
        confirm_trash_files = (
            len(list(trash.rglob("*.png"))) if trash.is_dir() else 0
        )
        confirm_undo_stack = len(service.undo_stack)

        # 2) skip：零移动。
        skip_a = _make_image(root / "skip_a.png", (32, 32), (220, 80, 20))
        skip_b = _make_image(root / "skip_b.png", (32, 32), (220, 80, 20))
        shutil.copyfile(skip_a, skip_b)
        skip_group = _group(
            "exact", _info(skip_a, "skip_a.png"), _info(skip_b, "skip_b.png")
        )
        skip_dialog = review_qt.QtReviewDialog(skip_group, 1, 1)
        skip_dialog._finish("skip")
        skip_action, skip_keeper = skip_dialog.decision
        skip_outcome = service.apply_decision(
            skip_group, skip_action, skip_keeper
        )
        skip_files = [skip_a.is_file(), skip_b.is_file()]

        # 3) suspend：零移动并标记挂起。
        sus_a = _make_image(root / "sus_a.png", (16, 16), (120, 20, 160))
        sus_b = _make_image(root / "sus_b.png", (16, 16), (120, 20, 160))
        shutil.copyfile(sus_a, sus_b)
        sus_group = _group(
            "exact", _info(sus_a, "sus_a.png"), _info(sus_b, "sus_b.png")
        )
        sus_dialog = review_qt.QtReviewDialog(sus_group, 1, 1)
        sus_dialog._finish("suspend")
        sus_action, sus_keeper = sus_dialog.decision
        sus_outcome = service.apply_decision(sus_group, sus_action, sus_keeper)

        return {
            "ok": True,
            "confirm_action": action,
            "confirm_moves": confirm_moves,
            "confirm_trash_files": confirm_trash_files,
            "confirm_undo_stack": confirm_undo_stack,
            "skip_action": skip_action,
            "skip_moves": len(skip_outcome.moves),
            "skip_files_still_direct": skip_files,
            "suspend_action": sus_action,
            "suspend_moves": len(sus_outcome.moves),
            "suspend_flag": sus_outcome.suspended,
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_e_thread_and_signal_chain() -> dict:
    _enable_offscreen()
    import review_qt
    from scan_service import ScanService
    from pixfilter_core import TRASH_DIR_NAME
    from PySide6.QtCore import QThread, Qt, Signal

    app = review_qt.ensure_qapplication()
    root = Path(tempfile.mkdtemp(prefix="pixfilter_qt_chain_"))
    try:
        # 1) 工作线程直接创建 QApplication / 弹窗必须报错。
        first = _make_image(root / "chain_a.png", (64, 48), (40, 160, 90))
        second = _make_image(root / "chain_b.png", (64, 48), (40, 160, 90))
        shutil.copyfile(first, second)
        group = _group(
            "exact", _info(first, "chain_a.png"), _info(second, "chain_b.png")
        )
        worker_errors: list[str] = []

        def worker() -> None:
            try:
                review_qt.ensure_qapplication()
            except Exception as exc:
                worker_errors.append(type(exc).__name__)
            try:
                review_qt.QtReviewDialog(group, 1, 1)
            except Exception as exc:
                worker_errors.append(type(exc).__name__)

        thread = threading.Thread(target=worker, name="qt-worker-guard")
        thread.start()
        thread.join(3.0)

        # 2) QThread + Signal → 主线程槽。
        qthread_seen: dict[str, object] = {}

        class Emitter(QThread):
            payload = Signal(object)

            def run(self) -> None:  # noqa: N802
                self.payload.emit("from-qthread")

        emitter = Emitter()

        def on_qthread(value) -> None:
            qthread_seen["thread"] = threading.current_thread().name
            qthread_seen["value"] = value

        emitter.payload.connect(on_qthread, Qt.ConnectionType.QueuedConnection)
        emitter.start()
        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline and "value" not in qthread_seen:
            app.processEvents()
            time.sleep(0.01)
        emitter.wait(2000)

        # 3) 真实服务线程 → queued signal → 弹窗 → submit_decision。
        finished: dict[str, object] = {}
        handler_threads: list[str] = []
        active: dict[str, object] = {}

        def _open(g, index, total, scan_root) -> None:
            handler_threads.append(threading.current_thread().name)
            dialog = review_qt.QtReviewDialog(g, index, total)
            _show_offscreen(dialog)
            active["dialog"] = dialog

            def auto_confirm() -> None:
                if dialog.decision is None:
                    dialog._finish("confirm")

            from PySide6.QtCore import QTimer

            QTimer.singleShot(40, auto_confirm)

        bridge = review_qt.QtReviewBridge(_open)
        service = ScanService(
            group_ready_cb=bridge.post,
            finished_cb=lambda report: finished.update(report=report),
        )
        started = service.start_scan(root, 0, max_workers=2)
        deadline = time.perf_counter() + 8.0
        while time.perf_counter() < deadline:
            app.processEvents()
            dialog = active.get("dialog")
            if dialog is not None and getattr(dialog, "decision", None) is not None:
                action, keeper = dialog.decision
                active.pop("dialog", None)
                service.submit_decision(action, keeper)
            if "report" in finished and service.state == "idle":
                break
            time.sleep(0.01)
        service.stop()

        trash = root / TRASH_DIR_NAME
        return {
            "ok": True,
            "worker_errors": worker_errors,
            "qthread_slot_thread": qthread_seen.get("thread"),
            "qthread_value": qthread_seen.get("value"),
            "service_started": started,
            "handler_thread": handler_threads[0] if handler_threads else None,
            "finished_once": len(finished) == 1,
            "final_state": service.state,
            "moves": len(getattr(finished.get("report"), "moves", [])),
            "trash_files": len(list(trash.rglob("*.png"))) if trash.is_dir() else 0,
            "undo_batches": len(service.undo_stack),
        }
    finally:
        shutil.rmtree(root, ignore_errors=True)


def case_ui_polish() -> dict:
    """对比弹窗 UI 美化回归：居中、卡片质感/阴影、选中态、按钮三态、逻辑不回归。"""
    _enable_offscreen()
    import gc
    import re

    import review_qt
    from scan_service import ScanService
    from pixfilter_core import TRASH_DIR_NAME
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtWidgets import QGraphicsDropShadowEffect
    from PySide6.QtTest import QTest
    from config import (
        DIALOG_CONTENT_PAD,
        REVIEW_BUTTON_RADIUS,
        REVIEW_CARD_BORDER,
        REVIEW_CARD_BORDER_WIDTH,
        REVIEW_CARD_RADIUS,
        REVIEW_CARD_SELECTED_BORDER,
        REVIEW_CARD_SELECTED_BORDER_WIDTH,
        REVIEW_CARD_SELECTED_HOVER_BORDER,
        REVIEW_CARD_SELECTED_BG,
        REVIEW_CARD_SHADOW_PAD,
        REVIEW_CARD_WIDTH,
        DIALOG_CARD_PADX,
        REVIEW_PRIMARY_DISABLED_BG,
        REVIEW_SECONDARY_BORDER,
    )

    app = review_qt.ensure_qapplication()
    root = Path(tempfile.mkdtemp(prefix="pixfilter_qt_polish_"))
    dialogs = []
    try:
        def make(name: str, size) -> Path:
            return _make_image(root / name, size, (30, 120, 200))

        first = make("polish_a.png", (800, 600))
        second = make("polish_b.png", (640, 480))
        group = _group(
            "exact",
            _info(first, "polish_a.png"),
            _info(second, "polish_b.png"),
        )
        dialog = review_qt.QtReviewDialog(group, 1, 1)
        dialogs.append(dialog)
        _show_offscreen(dialog)
        app.processEvents()

        def measure(target, width: int) -> dict:
            target.resize(width, 700)
            app.processEvents()
            target.layout().activate()
            vp = target._scroll.viewport()
            row = target._card_row
            cards = [
                row.itemAt(i).widget()
                for i in range(row.count())
                if row.itemAt(i).widget()
            ]
            left = cards[0].mapTo(vp, QPoint(0, 0)).x()
            right = vp.width() - (
                cards[-1].mapTo(vp, QPoint(0, 0)).x() + cards[-1].width()
            )
            return {
                "left": left,
                "right": right,
                "diff": abs(left - right),
                "viewport": vp.width(),
                "card_widths": [card.width() for card in cards],
                "scroll_max": target._scroll.horizontalScrollBar().maximum(),
            }

        centering = {"900": measure(dialog, 900), "1400": measure(dialog, 1400)}

        # 多卡片：内容超宽时仍可横向滚动，卡片不被压缩或裁切。
        wide_paths = [
            make(f"polish_w{i}.png", (640 - i * 40, 480 - i * 30))
            for i in range(4)
        ]
        wide_group = _group(
            "exact",
            _info(wide_paths[0], "polish_w0.png"),
            *[_info(path, path.name) for path in wide_paths[1:]],
        )
        wide_dialog = review_qt.QtReviewDialog(wide_group, 1, 1)
        dialogs.append(wide_dialog)
        _show_offscreen(wide_dialog)
        app.processEvents()
        overflow = measure(wide_dialog, 900)
        expected_width = (
            len(wide_paths) * REVIEW_CARD_WIDTH
            + (len(wide_paths) - 1) * DIALOG_CARD_PADX * 2
            + REVIEW_CARD_SHADOW_PAD * 2
        )
        overflow.update(
            {
                "container_width": wide_dialog._container.width(),
                "expected_width": expected_width,
                "no_clip": all(
                    width == REVIEW_CARD_WIDTH
                    for width in overflow["card_widths"]
                )
                and wide_dialog._container.width() >= expected_width,
                "start_gap": overflow["left"],
                "starts_at_margin": overflow["left"] == REVIEW_CARD_SHADOW_PAD,
            }
        )

        # 卡片质感 + 阴影强引用。
        default_card = dialog._cards[str(first)]
        card_style = default_card.styleSheet()
        margins = default_card.layout().contentsMargins()
        shadow = default_card.graphicsEffect()
        gc.collect()
        card_info = {
            "radius_ok": f"border-radius: {REVIEW_CARD_RADIUS}px" in card_style,
            "selected_border_ok": (
                f"border: {REVIEW_CARD_SELECTED_BORDER_WIDTH}px solid"
                f" {REVIEW_CARD_SELECTED_BORDER}" in card_style
            ),
            "selected_bg_ok": REVIEW_CARD_SELECTED_BG in card_style,
            "hover_ok": (
                ":hover" in card_style
                and REVIEW_CARD_SELECTED_HOVER_BORDER in card_style
            ),
            "padding_ok": all(
                getattr(margins, side)() >= DIALOG_CONTENT_PAD
                for side in ("left", "top", "right", "bottom")
            ),
            "cursor_ok": (
                default_card.cursor().shape()
                == Qt.CursorShape.PointingHandCursor
            ),
            "hover_attr": default_card.testAttribute(
                Qt.WidgetAttribute.WA_Hover
            ),
            "shadow_is_effect": isinstance(shadow, QGraphicsDropShadowEffect),
            "shadow_blur": shadow.blurRadius() if shadow else 0.0,
            "shadow_offset": (
                [shadow.offset().x(), shadow.offset().y()] if shadow else []
            ),
            "shadow_ref_kept": any(
                item is shadow for item in dialog._card_effects
            ),
            "shadow_after_gc": default_card.graphicsEffect() is shadow,
        }

        # 选中切换：旧卡片恢复、新卡片高亮、单选行加粗。
        initial_selected = Path(dialog._selected_path()).name
        other_radio = dialog._radios[str(second)]
        other_radio.setChecked(True)
        app.processEvents()
        after_selected = Path(dialog._selected_path()).name
        old_card = dialog._cards[str(first)]
        new_card = dialog._cards[str(second)]
        selected_info = {
            "default": dialog.default.path.name,
            "initial": initial_selected,
            "after": after_selected,
            "old_property": bool(old_card.property("selected")),
            "new_property": bool(new_card.property("selected")),
            "old_style_reset": (
                f"border: {REVIEW_CARD_BORDER_WIDTH}px solid {REVIEW_CARD_BORDER}"
                in old_card.styleSheet()
                and f"{REVIEW_CARD_SELECTED_BORDER_WIDTH}px solid"
                not in old_card.styleSheet()
            ),
            "new_style_highlight": (
                f"border: {REVIEW_CARD_SELECTED_BORDER_WIDTH}px solid"
                f" {REVIEW_CARD_SELECTED_BORDER}" in new_card.styleSheet()
            ),
            "old_bold": dialog._radios[str(first)].font().bold(),
            "new_bold": other_radio.font().bold(),
            "radio_text": other_radio.text(),
            "radio_indicator": (
                "QRadioButton::indicator" in other_radio.styleSheet()
                and "border-radius" in other_radio.styleSheet()
            ),
            "focus_state": "QRadioButton:focus" in other_radio.styleSheet(),
        }

        # 卡片点击选中 vs 缩略图点击开大图。
        QTest.mouseClick(
            dialog._status_labels[str(first)], Qt.MouseButton.LeftButton
        )
        app.processEvents()
        card_click = {
            "selected": Path(dialog._selected_path()).name,
            "viewers": len(dialog._viewers),
        }
        QTest.mouseClick(dialog._thumbnail_labels[0], Qt.MouseButton.LeftButton)
        app.processEvents()
        thumb_click = {
            "selected": Path(dialog._selected_path()).name,
            "viewers": len(dialog._viewers),
        }
        viewer = dialog._viewers[0] if dialog._viewers else None
        viewer_space_closed = False
        if viewer is not None:
            _show_offscreen(viewer)
            app.processEvents()
            QTest.keyClick(viewer, Qt.Key.Key_Space)
            app.processEvents()
            viewer_space_closed = not viewer.isVisible()

        # 底部按钮三态：读 QSS 规则里的背景/边框色。
        def button_states(qss: str) -> dict:
            states = {}
            for state, body in re.findall(
                r"QPushButton(?::(\w+))?\s*\{([^}]*)\}", qss
            ):
                background = re.search(
                    r"background-color:\s*(#[0-9a-fA-F]{6})", body
                )
                border = re.search(
                    r"border(?:-color)?:\s*(?:1px solid )?(#[0-9a-fA-F]{6})",
                    body,
                )
                states[state or "base"] = {
                    "bg": background.group(1) if background else None,
                    "border": border.group(1) if border else None,
                }
            return states

        primary_qss = dialog.btn_confirm.styleSheet()
        secondary_qss = dialog.btn_skip.styleSheet()
        primary_states = button_states(primary_qss)
        secondary_states = button_states(secondary_qss)
        primary_backgrounds = [
            primary_states[state]["bg"]
            for state in ("base", "hover", "pressed", "disabled")
        ]
        secondary_backgrounds = [
            secondary_states[state]["bg"]
            for state in ("base", "hover", "pressed", "disabled")
        ]
        dialog.btn_confirm.setEnabled(False)
        QTest.mouseClick(dialog.btn_confirm, Qt.MouseButton.LeftButton)
        app.processEvents()
        disabled_noop = dialog.decision is None
        dialog.btn_confirm.setEnabled(True)
        buttons = {
            "primary_radius_ok": (
                f"border-radius: {REVIEW_BUTTON_RADIUS}px" in primary_qss
            ),
            "primary_distinct": len(set(primary_backgrounds)) == 4,
            "primary_disabled_gray": (
                primary_states["disabled"]["bg"] == REVIEW_PRIMARY_DISABLED_BG
            ),
            "secondary_radius_ok": (
                f"border-radius: {REVIEW_BUTTON_RADIUS}px" in secondary_qss
            ),
            "secondary_border_1px": (
                "border: 1px solid" in secondary_qss
                and secondary_states["base"]["border"]
                == REVIEW_SECONDARY_BORDER
            ),
            "secondary_distinct": len(set(secondary_backgrounds)) == 4,
            "disabled_click_noop": disabled_noop,
            "primary_states": primary_states,
        }

        # 占位卡片样式 + 日志。
        missing = root / "missing.png"
        from pixfilter_core import ImageInfo

        missing_info = ImageInfo(
            path=missing,
            rel_path=Path("missing.png"),
            file_size=0,
            width=0,
            height=0,
        )
        logs: list[tuple[str, str]] = []
        placeholder_group = _group(
            "exact", _info(first, "polish_a.png"), missing_info
        )
        placeholder_dialog = review_qt.QtReviewDialog(
            placeholder_group,
            1,
            1,
            log=lambda message, kind="info": logs.append((kind, message)),
        )
        dialogs.append(placeholder_dialog)
        placeholder = placeholder_dialog._placeholders[0]
        placeholder_style = placeholder.styleSheet()
        joined_log = "\n".join(message for _kind, message in logs)
        placeholder_info = {
            "count": len(placeholder_dialog._placeholders),
            "single_failure_keeps_other": (
                len(placeholder_dialog._thumbnail_labels) == 1
            ),
            "text_ok": "无法加载图片" in placeholder.text(),
            "style_ok": (
                "border-radius" in placeholder_style
                and "background-color" in placeholder_style
            ),
            "log_ok": (
                str(missing) in joined_log and "FileNotFoundError" in joined_log
            ),
        }
        placeholder_dialog._finish("skip")

        # 三种决策仍映射到文件去向（确认移动 / 跳过 / 挂起都不改语义）。
        service = ScanService()
        service.folder = root

        def decision_case(action: str, prefix: str) -> dict:
            left = make(f"{prefix}_a.png", (32, 32))
            right = _make_image(root / f"{prefix}_b.png", (32, 32), (32, 32, 32))
            shutil.copyfile(left, right)
            target = _group(
                "exact",
                _info(left, left.name),
                _info(right, right.name),
            )
            target_dialog = review_qt.QtReviewDialog(target, 1, 1)
            dialogs.append(target_dialog)
            target_dialog._finish(action)
            keeper = target_dialog.decision[1]
            outcome = service.apply_decision(target, action, keeper)
            return {
                "action": target_dialog.decision[0],
                "moves": len(outcome.moves),
                "suspended": bool(outcome.suspended),
                "files_still_direct": [left.is_file(), right.is_file()],
            }

        confirm_result = decision_case("confirm", "dec_confirm")
        skip_result = decision_case("skip", "dec_skip")
        suspend_result = decision_case("suspend", "dec_suspend")
        trash = root / TRASH_DIR_NAME
        confirm_result["trash_files"] = (
            len(list(trash.rglob("dec_confirm_*.png")))
            if trash.is_dir()
            else 0
        )
        confirm_result["undo_batches"] = len(service.undo_stack)

        wide_dialog._finish("skip")
        dialog._finish("skip")

        return {
            "ok": True,
            "centering": centering,
            "overflow": overflow,
            "card": card_info,
            "selected": selected_info,
            "click": {"card": card_click, "thumbnail": thumb_click},
            "viewer_space_closed": viewer_space_closed,
            "buttons": buttons,
            "placeholder": placeholder_info,
            "decisions": {
                "confirm": confirm_result,
                "skip": skip_result,
                "suspend": suspend_result,
            },
        }
    finally:
        for dialog in dialogs:
            if getattr(dialog, "decision", None) is None:
                try:
                    dialog._finish("skip")
                except Exception:
                    pass
        shutil.rmtree(root, ignore_errors=True)


CASES = {
    "a": case_a_import_offscreen,
    "b": case_b_cards_and_pixmaps,
    "c": case_c_missing_placeholder,
    "d": case_d_decision_mapping,
    "e": case_e_thread_and_signal_chain,
    "ui_polish": case_ui_polish,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in CASES:
        print(f"usage: {sys.argv[0]} <{'|'.join(CASES)}>", file=sys.stderr)
        return 2
    try:
        payload = CASES[sys.argv[1]]()
    except Exception:
        _emit(
            {
                "ok": False,
                "case": sys.argv[1],
                "error": traceback.format_exc(),
            }
        )
        return 1
    payload.setdefault("case", sys.argv[1])
    _emit(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
