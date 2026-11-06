"""批处理复核工作台的独立子进程用例（a–j）。

运行：``QT_QPA_PLATFORM=offscreen python tests/qt_workbench_cases.py <case>``
成功时最后一行输出 JSON：``{"ok": true, ...}``。每个用例失败即抛异常。
"""

from __future__ import annotations

import gc
import inspect
import json
import os
import sys
import tempfile
import threading
import time
import weakref
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import (  # noqa: E402
    QAbstractAnimation,
    QEvent,
    QObject,
    QPoint,
    QPointF,
    QRect,
    QThread,
    Qt,
    QTimer,
)
from PySide6.QtGui import QColor, QGuiApplication, QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QLabel,
    QRadioButton,
    QSizePolicy,
    QStyledItemDelegate,
    QWidget,
)

import config  # noqa: E402
import scan_service  # noqa: E402
from pixfilter_core import (  # noqa: E402
    TRASH_DIR_NAME,
    DuplicateGroup,
    ImageInfo,
    ScanReport,
    format_size,
    group_members,
    pick_default_keeper,
)
from review_workbench_qt import (  # noqa: E402
    DONE_ROLE,
    STATUS_CONFIRMED,
    STATUS_DISCARDED,
    STATUS_PENDING,
    STATUS_SKIPPED,
    STATUS_SUSPENDED,
    QtReviewWorkbench,
)
from review_qt import ensure_qapplication  # noqa: E402

APP = None


def app() -> QApplication:
    global APP
    APP = ensure_qapplication()
    # 硬性要求：所有 UI 测试都跑在 offscreen 子进程里，绝不创建可见窗口。
    assert QGuiApplication.platformName() == "offscreen", (
        QGuiApplication.platformName()
    )
    return APP


def show_offscreen(widget) -> None:
    """离屏显示：即便平台插件意外回退，也不会把窗口投到真实桌面。"""
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()


def pump(seconds: float, predicate=None) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app().processEvents()
        if predicate is not None and predicate():
            app().processEvents()
            return True
        time.sleep(0.004)
    app().processEvents()
    return predicate() if predicate is not None else True


def send_left(widget, kind, pos=None) -> None:
    """向控件发送合成鼠标事件（控件内逻辑坐标，不涉及屏幕坐标）。"""
    if pos is None:
        pos = widget.rect().center()
    left = Qt.MouseButton.LeftButton
    event = QMouseEvent(
        kind,
        QPointF(pos),
        QPointF(pos),
        left,
        left,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def click_card(card) -> None:
    send_left(card, QEvent.Type.MouseButtonPress)
    send_left(card, QEvent.Type.MouseButtonRelease)


def rgb(color) -> tuple[int, int, int]:
    return (color.red(), color.green(), color.blue())


def color_bbox(pixmap, target, tolerance: int = 6):
    """匹配目标色的像素包围盒（返回逻辑坐标）。"""
    image = pixmap.toImage()
    ratio = pixmap.devicePixelRatio() or 1.0
    xs: list[float] = []
    ys: list[float] = []
    for y in range(image.height()):
        for x in range(image.width()):
            color = image.pixelColor(x, y)
            if color.alpha() < 200:
                continue
            if (
                abs(color.red() - target[0]) <= tolerance
                and abs(color.green() - target[1]) <= tolerance
                and abs(color.blue() - target[2]) <= tolerance
            ):
                xs.append(x / ratio)
                ys.append(y / ratio)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def temp_base() -> Path:
    return Path(tempfile.mkdtemp(prefix="pixfilter-workbench-"))


def make_png(path: Path, color=(180, 30, 30), size=(64, 64)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def make_group(
    base: Path, names: list[str], *, kind: str = "exact", folder: str = ""
) -> DuplicateGroup:
    infos: list[ImageInfo] = []
    for index, name in enumerate(names):
        rel = Path(folder) / name if folder else Path(name)
        path = base / rel
        if not path.exists():
            make_png(path, color=(180, 30 + index * 12, 30))
        infos.append(
            ImageInfo(
                path=path,
                rel_path=rel,
                file_size=path.stat().st_size,
                width=64,
                height=64,
            )
        )
    return DuplicateGroup(kind=kind, keeper=infos[0], duplicates=infos[1:])


def make_workbench(
    groups: list[DuplicateGroup], base: Path, decide=None, undo_group=None
) -> QtReviewWorkbench:
    logs: list[tuple[str, str]] = []
    workbench = QtReviewWorkbench(
        groups,
        base,
        log=lambda message, kind="info": logs.append((message, kind)),
        decide=decide,
        undo_group=undo_group,
    )
    show_offscreen(workbench)
    pump(0.35)
    return workbench


def make_service(base: Path) -> scan_service.ScanService:
    service = scan_service.ScanService()
    service.folder = base
    return service


def release_events() -> None:
    app().sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
    app().processEvents()


# ---------- a) 布局比例与最小宽度 ----------
def case_layout() -> dict:
    base = temp_base()
    groups = [make_group(base, [f"l{i}a.png", f"l{i}b.png"], folder=f"g{i}") for i in range(2)]
    workbench = make_workbench(groups, base)
    workbench.resize(1100, 720)
    pump(0.3)
    left = workbench.splitter.widget(0)
    right = workbench.splitter.widget(1)
    total = left.width() + right.width()
    left_ratio = left.width() / total
    right_ratio = right.width() / total
    assert 0.25 <= left_ratio <= 0.33, left_ratio
    assert 0.67 <= right_ratio <= 0.75, right_ratio

    workbench.resize(900, 600)
    pump(0.3)
    assert left.width() >= config.WORKBENCH_LEFT_MIN_WIDTH
    left_global = QRect(left.mapToGlobal(QPoint(0, 0)), left.size())
    right_global = QRect(right.mapToGlobal(QPoint(0, 0)), right.size())
    assert not left_global.intersects(right_global)

    # 用 setSizes 模拟“拖到极端”，不发送鼠标事件。
    workbench.splitter.setSizes([0, 10000])
    pump(0.15)
    assert left.width() >= config.WORKBENCH_LEFT_MIN_WIDTH, left.width()
    assert right.width() >= config.WORKBENCH_RIGHT_MIN_WIDTH, right.width()
    workbench.splitter.setSizes([10000, 0])
    pump(0.15)
    assert left.width() >= config.WORKBENCH_LEFT_MIN_WIDTH, left.width()
    assert right.width() >= config.WORKBENCH_RIGHT_MIN_WIDTH, right.width()

    buttons = (
        workbench.btn_skip,
        workbench.btn_undo_group,
        workbench.btn_discard,
        workbench.btn_confirm,
    )
    button_min = workbench._px(config.WORKBENCH_BUTTON_MIN_WIDTH)
    assert workbench.footer_hint.wordWrap() is True
    hint_policy = workbench.footer_hint.sizePolicy()
    assert hint_policy.horizontalPolicy() == QSizePolicy.Policy.Ignored
    assert hint_policy.hasHeightForWidth() is True
    # 提示独占一行、位于按钮区上方，且不属于按钮的水平布局。
    assert workbench.footer_layout.itemAt(0).widget() is workbench.footer_hint
    assert workbench.footer_layout.itemAt(1).layout() is workbench.button_row
    assert workbench.button_row.indexOf(workbench.footer_hint) == -1
    hint_bottom = workbench.footer_hint.mapTo(
        workbench, QPoint(0, workbench.footer_hint.height())
    ).y()
    button_top = workbench.btn_skip.mapTo(workbench, QPoint(0, 0)).y()
    assert hint_bottom <= button_top, (hint_bottom, button_top)
    for button in buttons:
        parent = button.parentWidget()
        assert button.geometry().right() <= parent.width(), (
            button.text(),
            button.geometry(),
            parent.width(),
        )
        assert button.geometry().left() >= 0
        assert button.width() >= button_min, (button.text(), button.width())
        assert button.isVisible()
    workbench.close()
    release_events()
    return {
        "left_ratio": left_ratio,
        "right_ratio": right_ratio,
        "min_left_width": left.width(),
        "min_right_width": right.width(),
        "button_widths": [button.width() for button in buttons],
        "button_min": button_min,
        "word_wrap": workbench.footer_hint.wordWrap(),
        "footer_separate": True,
    }


# ---------- b) 左栏纯文本 ----------
def case_list_text() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["a.png", "b.png", "c.png"], folder="g0"),
        make_group(base, ["d.png", "e.png"], folder="g1"),
        make_group(base, ["f.png", "g.png"], folder="g2"),
    ]
    workbench = make_workbench(groups, base)
    assert workbench.list_widget.count() == 3
    assert isinstance(workbench.list_widget.itemDelegate(), QStyledItemDelegate)
    paint_source = inspect.getsource(type(workbench.list_delegate).paint)
    build_source = inspect.getsource(type(workbench)._build_left)
    assert "QPixmap" not in paint_source and "setIcon" not in paint_source
    assert "QPixmap" not in build_source and "setIcon" not in build_source
    for item in workbench._list_items:
        assert item.icon().isNull()
        assert item.sizeHint().height() == config.WORKBENCH_LIST_ITEM_HEIGHT
    lines = workbench._list_items[0].text().splitlines()
    assert len(lines) == 3, lines
    assert lines[0] == "第 1/3 组 ⏳待处理"
    assert "a.png" in lines[1] and "·" in lines[1]
    assert "b.png" in lines[2] and lines[2].endswith("等 3 张")
    assert format_size(groups[0].keeper.file_size) in lines[1]
    labels = workbench.splitter.widget(0).findChildren(QLabel)
    assert all(label.pixmap().isNull() for label in labels), [l.text() for l in labels]
    assert workbench._pixmaps == [] or len(workbench._pixmaps) <= len(
        workbench.members
    )
    workbench.close()
    release_events()
    return {
        "item_count": workbench.list_widget.count(),
        "first_line": lines[0],
        "third_line": lines[2],
        "icon_null": True,
        "left_ratio_pixmaps": 0,
    }


# ---------- c) 选中/已处理样式 ----------
def case_styles() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["s0a.png", "s0b.png"], folder="g0"),
        make_group(base, ["s1a.png", "s1b.png"], folder="g1"),
        make_group(base, ["s2a.png", "s2b.png"], folder="g2"),
    ]
    service = make_service(base)
    workbench = make_workbench(
        groups, base, decide=lambda g, a, k: service.apply_decision(g, a, k)
    )
    delegate = workbench.list_delegate
    assert delegate.selected_bg == config.WORKBENCH_LIST_SELECTED_BG
    assert delegate.hover_bg == config.WORKBENCH_LIST_HOVER_BG
    assert delegate.hover_bg != delegate.selected_bg
    assert delegate.radius == config.WORKBENCH_LIST_RADIUS
    assert delegate.radius > 0
    assert delegate.selected_border_width == 0
    assert delegate.bar_color == config.WORKBENCH_LIST_SELECTED_BAR_COLOR
    assert delegate.bar_width == config.WORKBENCH_LIST_SELECTED_BAR_WIDTH
    assert delegate.done_fg == config.WORKBENCH_LIST_DONE_FG
    assert workbench.list_widget.currentRow() == 0
    assert workbench._status[0] == STATUS_PENDING

    workbench._confirm()
    pump(0.2)
    item = workbench._list_items[0]
    assert "✅ 已处理" in item.text()
    assert bool(item.data(DONE_ROLE)) is True
    assert workbench._status[0] == STATUS_CONFIRMED
    assert workbench.list_widget.currentRow() == 1
    assert (base / TRASH_DIR_NAME / "g0" / "s0b.png").is_file()
    workbench.close()
    release_events()
    return {
        "selected_bg": delegate.selected_bg,
        "hover_bg": delegate.hover_bg,
        "selected_width": delegate.selected_border_width,
        "bar_color": delegate.bar_color,
        "bar_width": delegate.bar_width,
        "radius": delegate.radius,
        "done_fg": delegate.done_fg,
        "processed_icon": "✅",
        "current_row": workbench.list_widget.currentRow(),
    }


# ---------- d) 保留/跳过后的自动推进 ----------
def case_advance() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["a1a.png", "a1b.png"], folder="g0"),
        make_group(base, ["a2a.png", "a2b.png"], folder="g1"),
        make_group(base, ["a3a.png", "a3b.png"], folder="g2"),
    ]
    service = make_service(base)
    workbench = make_workbench(
        groups, base, decide=lambda g, a, k: service.apply_decision(g, a, k)
    )
    workbench._skip()
    assert workbench._status[0] == STATUS_SKIPPED
    assert workbench.list_widget.currentRow() == 1
    workbench._confirm()
    assert workbench._status[1] == STATUS_CONFIRMED
    assert workbench.list_widget.currentRow() == 2
    workbench._skip()
    assert workbench._complete is True
    assert workbench.stack.currentWidget() is workbench.empty_page
    assert workbench.empty_title.text() == "全部处理完毕"
    assert "确认 1 组" in workbench.empty_summary.text()
    assert "跳过 2 组" in workbench.empty_summary.text()
    assert "移动 1 张" in workbench.empty_summary.text()
    assert not (base / TRASH_DIR_NAME / "g0" / "a1b.png").exists()
    assert (base / TRASH_DIR_NAME / "g1" / "a2b.png").is_file()
    workbench.close()
    release_events()
    return {
        "complete": workbench._complete,
        "summary": workbench.empty_summary.text(),
        "stack_is_empty": True,
    }


# ---------- e) 点击任意项切换右侧 ----------
def case_switch() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["w0a.png", "w0b.png"], folder="g0"),
        make_group(base, ["w1a.png", "w1b.png", "w1c.png"], folder="g1"),
        make_group(base, ["w2a.png", "w2b.png"], folder="g2"),
    ]
    workbench = make_workbench(groups, base)
    workbench.resize(1100, 720)
    pump(0.2)

    def click_row(row: int) -> None:
        item = workbench._list_items[row]
        rect = workbench.list_widget.visualItemRect(item)
        QTest.mouseClick(
            workbench.list_widget.viewport(),
            Qt.MouseButton.LeftButton,
            pos=rect.center(),
        )
        pump(0.2)

    click_row(2)
    assert workbench._current_index == 2
    assert len(workbench._cards) == 2
    assert workbench._selected_path() == str(pick_default_keeper(groups[2]).path)
    assert f"第 3/3 组" in workbench.right_title.text()
    click_row(1)
    assert workbench._current_index == 1
    assert len(workbench._cards) == 3
    assert workbench._selected_path() == str(pick_default_keeper(groups[1]).path)
    assert all(
        card.width() == config.REVIEW_CARD_WIDTH
        for card in workbench._cards.values()
    )
    workbench.close()
    release_events()
    return {
        "cards_after_row2": 2,
        "cards_after_row1": 3,
        "current_index": workbench._current_index,
    }


# ---------- f) 切换 5 组后的内存约束 ----------
def case_memory() -> dict:
    groups = []
    base = temp_base()
    for index in range(5):
        groups.append(
            make_group(
                base,
                [f"m{index}a.png", f"m{index}b.png"],
                folder=f"g{index}",
            )
        )
    workbench = make_workbench(groups, base)
    old_refs: list = []
    for row in range(5):
        if row > 0:
            old_refs = [weakref.ref(pixmap) for pixmap in workbench._pixmaps]
            workbench._select_group(row)
            pump(0.15)
            release_events()
            assert all(ref() is None for ref in old_refs), [
                ref() for ref in old_refs if ref() is not None
            ]
        assert len(workbench._pixmaps) <= len(workbench.members), (
            len(workbench._pixmaps),
            len(workbench.members),
        )
        assert len(workbench._cards) == len(workbench.members)
    release_events()
    alive_for_current = len(workbench._pixmaps)
    current_count = len(workbench.members)
    workbench.close()
    release_events()
    assert workbench._pixmaps == []
    return {
        "alive_for_current": alive_for_current,
        "current_count": current_count,
        "old_refs_dead": True,
    }


# ---------- g) 决策映射 / X 关闭挂起 / 零移动 ----------
def case_decisions() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["d0a.png", "d0b.png"], folder="g0"),
        make_group(base, ["d1a.png", "d1b.png"], folder="g1"),
        make_group(base, ["d2a.png", "d2b.png"], folder="g2"),
        make_group(base, ["d3a.png", "d3b.png"], folder="g3"),
    ]
    service = make_service(base)
    calls: list[tuple[str, int]] = []

    def decide(group, action, keeper):
        calls.append((action, groups.index(group)))
        return service.apply_decision(group, action, keeper)

    workbench = make_workbench(groups, base, decide=decide)
    workbench._confirm()
    pump(0.15)
    assert (base / TRASH_DIR_NAME / "g0" / "d0b.png").is_file()
    assert (base / "g0" / "d0a.png").is_file()
    assert calls[-1] == ("confirm", 0)

    workbench._skip()
    pump(0.15)
    assert not (base / TRASH_DIR_NAME / "g1" / "d1b.png").exists()
    assert (base / "g1" / "d1b.png").is_file()
    assert calls[-1] == ("skip", 1)

    # 已处理组再点确认不应二次移动（UI 状态位保护）。
    workbench._select_group(0)
    before = sorted(p.name for p in (base / TRASH_DIR_NAME).rglob("*") if p.is_file())
    workbench._decide_current("confirm", groups[0].keeper)
    after = sorted(p.name for p in (base / TRASH_DIR_NAME).rglob("*") if p.is_file())
    assert before == after
    assert workbench._status[0] == STATUS_CONFIRMED

    # X 关闭：剩余两组全部挂起且零移动。
    workbench.close()
    pump(0.2)
    assert workbench._status[2] == STATUS_SUSPENDED
    assert workbench._status[3] == STATUS_SUSPENDED
    assert all(action == "suspend" for action, _ in calls[-2:])
    assert not (base / TRASH_DIR_NAME / "g2" / "d2b.png").exists()
    assert not (base / TRASH_DIR_NAME / "g3" / "d3b.png").exists()
    assert (base / "g2" / "d2b.png").is_file()
    assert (base / "g3" / "d3b.png").is_file()
    release_events()
    return {
        "calls": calls,
        "confirm_moved": True,
        "skip_no_move": True,
        "suspend_no_move": True,
        "duplicate_no_move": True,
    }


# ---------- h) 服务层批处理模式 ----------
def _batch_report(base: Path, groups: list[DuplicateGroup]) -> ScanReport:
    return ScanReport(
        root=base,
        scanned=sum(group.total for group in groups),
        exact_groups=list(groups),
        skipped_images=2,
    )


def case_service_batch() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["h0a.png", "h0b.png"], folder="g0"),
        make_group(base, ["h1a.png", "h1b.png"], folder="g1"),
        make_group(base, ["h2a.png", "h2b.png"], folder="g2"),
        make_group(base, ["h3a.png", "h3b.png"], folder="g3"),
    ]
    report = _batch_report(base, groups)
    delivered: dict = {}
    finished: list = []
    review_done = threading.Event()
    groups_event = threading.Event()
    finished_event = threading.Event()

    def on_groups(all_groups, root):
        delivered["groups"] = list(all_groups)
        delivered["root"] = root
        groups_event.set()

    def on_review(summary):
        finished.append(summary)
        review_done.set()

    service = scan_service.ScanService(
        groups_ready_cb=on_groups,
        review_finished_cb=on_review,
        finished_cb=lambda _report: finished_event.set(),
        review_mode=scan_service.REVIEW_MODE_BATCH,
    )
    with mock.patch.object(scan_service, "scan_folder", return_value=report):
        assert service.start_scan(base, 8) is True
        assert groups_event.wait(5.0), "未一次性交出全部组"
        assert finished_event.wait(5.0), "扫描线程未结束"
    got = delivered["groups"]
    assert len(got) == 4
    assert service.state == "reviewing", service.state

    first = service.apply_decision(got[0], "confirm", got[0].keeper)
    duplicate = service.apply_decision(got[0], "confirm", got[0].keeper)
    assert duplicate.duplicate is True and duplicate.moves == []
    assert len(service.undo_stack) == 1, service.undo_stack
    service.apply_decision(got[1], "confirm", got[1].keeper)
    service.apply_decision(got[2], "skip", got[2].keeper)
    service.apply_decision(got[3], "suspend", got[3].keeper)
    assert review_done.wait(3.0), "未触发复核汇总"
    assert len(finished) == 1
    summary = finished[0]
    assert summary.total_groups == 4
    assert summary.confirmed_groups == 2
    assert summary.moved_files == 2
    assert summary.skipped_groups == 1
    assert summary.suspended_groups == 1
    assert summary.skipped_images == 2
    assert summary.cancelled is False
    assert service.state == "idle"
    assert len(service.pending_groups) == 1

    undo_events: list = []
    service._undo_finished_cb = lambda *args: undo_events.append(args)
    assert service.undo_last_batch() is True
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and service.state != "idle":
        time.sleep(0.01)
    assert service.state == "idle"
    assert (base / "g1" / "h1b.png").is_file(), "LIFO 未先恢复最后一组"
    assert (base / TRASH_DIR_NAME / "g0" / "h0b.png").is_file()
    assert service.undo_last_batch() is True
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline and service.state != "idle":
        time.sleep(0.01)
    assert (base / "g0" / "h0b.png").is_file()
    assert not service.has_undo

    # 取消：已确认保持，未确认全部挂起且零移动。
    base2 = temp_base()
    groups2 = [
        make_group(base2, ["c0a.png", "c0b.png"], folder="g0"),
        make_group(base2, ["c1a.png", "c1b.png"], folder="g1"),
        make_group(base2, ["c2a.png", "c2b.png"], folder="g2"),
    ]
    report2 = _batch_report(base2, groups2)
    delivered2: list = []
    review2: list = []
    groups2_event = threading.Event()
    review2_event = threading.Event()

    service2 = scan_service.ScanService(
        groups_ready_cb=lambda g, r: (delivered2.extend(g), groups2_event.set()),
        review_finished_cb=lambda s: (review2.append(s), review2_event.set()),
        review_mode=scan_service.REVIEW_MODE_BATCH,
    )
    with mock.patch.object(scan_service, "scan_folder", return_value=report2):
        assert service2.start_scan(base2, 8) is True
        assert groups2_event.wait(5.0)
    service2.apply_decision(delivered2[0], "confirm", delivered2[0].keeper)
    assert (base2 / TRASH_DIR_NAME / "g0" / "c0b.png").is_file()
    service2.request_cancel()
    assert review2_event.wait(3.0), "取消后未触发汇总"
    snapshot = review2[0]
    assert snapshot.cancelled is True
    assert snapshot.confirmed_groups == 1 and snapshot.moved_files == 1
    assert snapshot.suspended_groups == 2
    assert len(service2.pending_groups) == 2
    assert service2.state == "idle"
    assert (base2 / "g1" / "c1b.png").is_file()
    assert (base2 / "g2" / "c2b.png").is_file()
    assert not (base2 / TRASH_DIR_NAME / "g1" / "c1b.png").exists()
    return {
        "groups_once": len(got),
        "summary": {
            "confirmed": summary.confirmed_groups,
            "moved": summary.moved_files,
            "skipped": summary.skipped_groups,
            "suspended": summary.suspended_groups,
            "skipped_images": summary.skipped_images,
        },
        "duplicate_protected": True,
        "lifo_ok": True,
        "cancel_zero_move": True,
        "cancel_suspended": snapshot.suspended_groups,
    }


# ---------- i) 空状态、汇总文案与关闭清理 ----------
def case_empty_cleanup() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["e0a.png", "e0b.png"], folder="g0"),
        make_group(base, ["e1a.png", "e1b.png"], folder="g1"),
    ]
    service = make_service(base)
    workbench = make_workbench(
        groups, base, decide=lambda g, a, k: service.apply_decision(g, a, k)
    )
    workbench._skip()
    workbench._skip()
    assert workbench._complete
    assert workbench.stack.currentWidget() is workbench.empty_page
    assert workbench.empty_title.text() == "全部处理完毕"
    assert "确认 0 组" in workbench.empty_summary.text()
    assert "移动 0 张" in workbench.empty_summary.text()
    assert "跳过 2 组" in workbench.empty_summary.text()
    assert workbench._pixmaps == []
    assert workbench._cards == {}

    # 完成后点击已处理项：只读查看，按钮禁用，不得再移动。
    workbench._select_group(0)
    assert workbench.stack.currentWidget() is workbench.scroll
    assert len(workbench._cards) == 2
    assert not workbench.btn_confirm.isEnabled()
    assert not workbench.btn_skip.isEnabled()
    before = sorted(p.name for p in (base / TRASH_DIR_NAME).rglob("*") if p.is_file())
    assert before == []

    workbench.close()
    release_events()
    assert not workbench.isVisible()
    assert workbench._pixmaps == []
    assert [
        widget for widget in app().topLevelWidgets() if widget.isVisible()
    ] == []
    assert not [
        timer for timer in workbench.findChildren(QTimer) if timer.isActive()
    ]
    assert not [
        thread for thread in workbench.findChildren(QThread) if thread.isRunning()
    ]
    return {
        "title": workbench.empty_title.text(),
        "summary": workbench.empty_summary.text(),
        "readonly_disabled": True,
        "cleanup": True,
    }


# ---------- j) 卡片/按钮样式回归 ----------
def case_regression() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["r0a.png", "r0b.png"], folder="g0"),
        make_group(base, ["r1a.png", "r1b.png"], folder="g1"),
    ]
    workbench = make_workbench(groups, base)
    selected = workbench._cards[str(workbench._selected_path())]
    unselected_path = next(
        path for path in workbench._cards if path != workbench._selected_path()
    )
    unselected = workbench._cards[unselected_path]
    assert f"border-radius: {config.REVIEW_CARD_RADIUS}px" in selected.styleSheet()
    assert (
        f"border: {config.REVIEW_CARD_SELECTED_BORDER_WIDTH}px solid "
        f"{config.REVIEW_CARD_SELECTED_BORDER}"
    ) in selected.styleSheet()
    assert (
        f"border: {config.REVIEW_CARD_BORDER_WIDTH}px solid "
        f"{config.REVIEW_CARD_BORDER}"
    ) in unselected.styleSheet()
    assert selected.graphicsEffect() is not None
    assert selected in workbench._cards.values()
    assert all(effect is not None for effect in workbench._card_effects)
    primary = workbench.btn_confirm.styleSheet()
    assert f"border-radius: {config.REVIEW_BUTTON_RADIUS}px" in primary
    for token in (":hover", ":pressed", ":disabled"):
        assert token in primary
    secondary = workbench.btn_skip.styleSheet()
    assert "border: 1px solid" in secondary
    assert f"border-radius: {config.REVIEW_BUTTON_RADIUS}px" in secondary
    assert workbench.btn_confirm.text() == "保留选中的，移动其他图片"
    assert workbench.btn_skip.text() == "跳过"
    workbench.close()
    release_events()
    return {
        "card_radius": config.REVIEW_CARD_RADIUS,
        "selected_border": config.REVIEW_CARD_SELECTED_BORDER_WIDTH,
        "shadow_ok": True,
        "primary_tristate": True,
        "secondary_border_1px": True,
    }


# ---------- k) UI 美化：分界线 / 留白 / 选中态 / 项间距 / 居中 / 底部 ----------
def case_ui_polish() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["u0a.png", "u0b.png"], folder="g0"),
        make_group(base, ["u1a.png", "u1b.png"], folder="g1"),
    ]
    service = make_service(base)
    workbench = make_workbench(
        groups,
        base,
        decide=lambda g, a, k: service.apply_decision(g, a, k),
        undo_group=lambda g: service.undo_group(g),
    )
    workbench.resize(1100, 720)
    pump(0.4)
    px = workbench._px
    scale = workbench.ui_scale

    # a) 分界线：1px 浅灰 handle，无额外 QFrame 分割线。
    handle_width = workbench.splitter.handleWidth()
    assert handle_width == px(config.WORKBENCH_HANDLE_WIDTH), handle_width
    assert handle_width >= 1
    qss = workbench.splitter.styleSheet()
    assert f"background-color: {config.WORKBENCH_HANDLE_COLOR}" in qss
    assert "border: none" in qss
    separators = [
        frame
        for frame in workbench.splitter.findChildren(QFrame)
        if frame.frameShape() in (QFrame.Shape.HLine, QFrame.Shape.VLine)
    ]
    assert separators == [], separators

    # b) 左右面板留白 ≥ 20px（按 DPI）。
    left_panel = workbench.splitter.widget(0)
    right_panel = workbench.splitter.widget(1)
    expected_pad = px(config.WORKBENCH_PANEL_PAD)
    for panel in (left_panel, right_panel):
        margins = panel.layout().contentsMargins()
        for value in (
            margins.left(), margins.top(), margins.right(), margins.bottom()
        ):
            assert value >= expected_pad, (panel.objectName(), value, expected_pad)
    assert workbench.list_widget.geometry().left() >= expected_pad
    assert workbench.stack.geometry().left() >= expected_pad
    assert workbench.stack.geometry().right() <= (
        right_panel.width() - expected_pad
    )

    # c) 左栏选中态：浅蓝圆角底 + 2px 左条，无四周粗边。
    delegate = workbench.list_delegate
    assert delegate.selected_bg == config.WORKBENCH_LIST_SELECTED_BG
    assert delegate.hover_bg == config.WORKBENCH_LIST_HOVER_BG
    assert delegate.hover_bg != delegate.selected_bg
    assert delegate.radius > 0
    assert delegate.selected_border_width == 0
    assert delegate.bar_color == config.WORKBENCH_LIST_SELECTED_BAR_COLOR
    assert delegate.bar_width == 2
    assert delegate.done_fg != delegate.hover_bg
    assert workbench.list_widget.currentRow() == 0

    # d) 相邻两项的垂直间隙 ≥ 4px（含 DPI 缩放）。
    rects = [
        workbench.list_widget.visualItemRect(item)
        for item in workbench._list_items
    ]
    gaps = [
        rects[index + 1].top() - rects[index].bottom() - 1
        for index in range(len(rects) - 1)
    ]
    assert gaps and min(gaps) >= px(config.WORKBENCH_LIST_ITEM_MARGIN), gaps

    # e) 单组卡片垂直居中：上下留白差 ≤ 4px。
    viewport = workbench.scroll.viewport()
    cards = list(workbench._cards.values())
    assert len(cards) == 2
    tops = [card.mapTo(viewport, QPoint(0, 0)).y() for card in cards]
    top_gap = min(tops)
    bottom_gap = viewport.height() - (max(tops) + cards[0].height())
    assert abs(top_gap - bottom_gap) <= max(4, px(4)), (top_gap, bottom_gap)

    # f) 底部按钮：距窗口底边 ≥ 16px，两按钮间距 10±2px，三态样式仍在。
    button_bottom = workbench.btn_confirm.mapTo(
        workbench, QPoint(0, workbench.btn_confirm.height())
    ).y()
    assert workbench.height() - button_bottom >= px(16)
    skip = workbench.btn_skip
    undo = workbench.btn_undo_group
    discard = workbench.btn_discard
    confirm = workbench.btn_confirm
    # 四个按钮必须同一行（竖直中心一致、无换行），相邻间距都等于配置值。
    buttons = (skip, undo, discard, confirm)
    row_tops = {button.geometry().top() for button in buttons}
    assert len(row_tops) == 1, row_tops
    button_gaps = [
        buttons[index + 1].geometry().left()
        - (buttons[index].geometry().left() + buttons[index].geometry().width())
        for index in range(len(buttons) - 1)
    ]
    for button_gap in button_gaps:
        assert abs(button_gap - px(config.WORKBENCH_BUTTON_GAP)) <= 2, button_gap
    for button in buttons:
        sheet = button.styleSheet()
        assert f"border-radius: {config.REVIEW_BUTTON_RADIUS}px" in sheet
        for token in (":hover", ":pressed", ":disabled"):
            assert token in sheet, (button.text(), token)
    assert "border: 1px solid" in skip.styleSheet()
    assert discard.text() == config.WORKBENCH_DISCARD_TEXT
    assert config.REVIEW_DISCARD_BG in discard.styleSheet()
    assert config.REVIEW_DISCARD_FG in discard.styleSheet()
    # “撤销本组”初始禁用（尚未处理任何组）。
    assert undo.text() == config.WORKBENCH_UNDO_TEXT
    assert not undo.isEnabled()

    # g) 回归：自动跳转 / 空状态 / 只读禁用 / 跳过零移动。
    workbench._skip()
    assert workbench._status[0] == STATUS_SKIPPED
    assert workbench.list_widget.currentRow() == 1
    workbench._confirm()
    assert workbench._complete is True
    assert workbench.stack.currentWidget() is workbench.empty_page
    assert "跳过 1 组" in workbench.empty_summary.text()
    assert "确认 1 组" in workbench.empty_summary.text()
    assert not workbench.btn_confirm.isEnabled()
    assert not workbench.btn_skip.isEnabled()
    assert not workbench.btn_discard.isEnabled()
    # 最后一组刚发生过移动：空态下“撤销本组”按最近处理组点亮。
    assert workbench.btn_undo_group.isEnabled()
    assert workbench._undo_target_index() == 1
    assert not (base / TRASH_DIR_NAME / "g0" / "u0b.png").exists()
    workbench._select_group(0)
    assert workbench.stack.currentWidget() is workbench.scroll
    assert not workbench.btn_confirm.isEnabled()
    assert not workbench.btn_skip.isEnabled()
    assert not workbench.btn_discard.isEnabled()
    # 切到“已跳过”组时自身不可撤，但最近处理组的批次仍可撤。
    assert workbench.btn_undo_group.isEnabled()
    assert workbench._undo_target_index() == 1

    workbench.close()
    release_events()
    return {
        "handle_width": handle_width,
        "handle_color": config.WORKBENCH_HANDLE_COLOR,
        "separators": 0,
        "panel_pad": expected_pad,
        "selected_bg": delegate.selected_bg,
        "hover_bg": delegate.hover_bg,
        "selected_width": delegate.selected_border_width,
        "bar_width": delegate.bar_width,
        "radius": delegate.radius,
        "item_gap": min(gaps),
        "card_top_gap": top_gap,
        "card_bottom_gap": bottom_gap,
        "button_bottom_gap": workbench.height() - button_bottom,
        "button_gap": min(button_gaps),
        "primary_tristate": True,
        "secondary_border_1px": True,
        "complete": workbench._complete,
        "readonly_disabled": True,
        "skipped_no_move": True,
        "scale": scale,
    }


# ---------- l) 卡片控件树：无单选，仅缩略图 / 文件名 / 大小 ----------
def case_card_tree() -> dict:
    base = temp_base()
    group = make_group(
        base, ["tree_a.png", "tree_b.png", "tree_c.png"], folder="g"
    )
    workbench = make_workbench([group], base)
    assert workbench.findChildren(QRadioButton) == [], "卡片内仍有 QRadioButton"
    texts = [label.text() for label in workbench.findChildren(QLabel)]
    assert all("保留这一张" not in text for text in texts), texts
    members = group_members(group)
    cards = list(workbench._cards.values())
    assert len(cards) == len(members)
    sizes: dict[str, str] = {}
    for card, member in zip(cards, members):
        children = card.findChildren(QWidget)
        labels = card.findChildren(QLabel)
        assert len(children) == 3, [child.objectName() for child in children]
        assert [label.objectName() for label in labels] == [
            "cardThumb", "cardName", "cardSize",
        ]
        thumb, name, size = labels
        pixmap = thumb.pixmap()
        assert pixmap is not None and not pixmap.isNull()
        assert thumb.width() == config.THUMB_MAX
        assert thumb.height() == config.THUMB_MAX
        assert pixmap.width() <= config.THUMB_MAX
        assert pixmap.height() <= config.THUMB_MAX
        if member.width and member.height:
            source_ratio = member.width / member.height
            assert abs(pixmap.width() / pixmap.height() - source_ratio) < 0.02
        assert name.text() == member.path.name, name.text()
        assert size.text() == format_size(member.file_size), size.text()
        sizes[member.path.name] = size.text()
    workbench.close()
    release_events()
    return {
        "cards": len(cards),
        "children_per_card": 3,
        "radio_count": 0,
        "sizes": sizes,
    }


# ---------- m) 单击卡片切换选中；旧卡恢复；confirm keeper 正确 ----------
def case_card_selection() -> dict:
    base = temp_base()
    group = make_group(
        base, ["sel_a.png", "sel_b.png", "sel_c.png"], folder="g"
    )
    workbench = make_workbench([group], base)
    members = group_members(group)
    default = pick_default_keeper(group)
    assert workbench._selected_path() == str(default.path)
    other = next(
        member for member in members if str(member.path) != str(default.path)
    )
    old_card = workbench._cards[str(default.path)]
    new_card = workbench._cards[str(other.path)]
    selected_rgb = rgb(QColor(config.REVIEW_CARD_SELECTED_BG))
    normal_rgb = rgb(QColor(config.REVIEW_CARD_BG))

    click_card(new_card)
    pump(0.2)
    assert workbench._selected_path() == str(other.path)
    assert bool(old_card.property("selected")) is False
    assert bool(new_card.property("selected")) is True
    assert (
        f"border: {config.REVIEW_CARD_SELECTED_BORDER_WIDTH}px solid "
        f"{config.REVIEW_CARD_SELECTED_BORDER}"
    ) in new_card.styleSheet()
    assert f"background-color: {config.REVIEW_CARD_SELECTED_BG}" in (
        new_card.styleSheet()
    )
    assert f"background-color: {config.REVIEW_CARD_BG}" in old_card.styleSheet()

    def bottom_color(card):
        pixmap = card.grab()
        ratio = pixmap.devicePixelRatio() or 1.0
        image = pixmap.toImage()
        return rgb(
            image.pixelColor(
                int(round((card.width() // 2) * ratio)),
                int(round((card.height() - 16) * ratio)),
            )
        )

    assert bottom_color(new_card) == selected_rgb, bottom_color(new_card)
    assert bottom_color(old_card) == normal_rgb, bottom_color(old_card)

    calls: list[tuple[str, str]] = []

    def decide(_group, action, keeper) -> None:
        calls.append((action, str(keeper.path)))

    workbench._decide = decide
    workbench._confirm()
    assert calls == [("confirm", str(other.path))], calls
    workbench.close()
    release_events()
    return {
        "default": Path(str(default.path)).name,
        "selected": Path(str(other.path)).name,
        "old_restored": True,
        "confirm_keeper": Path(str(other.path)).name,
    }


# ---------- n) 按压动画：绘制缩 ≥1px，rect 不动，LayoutRequest 0 ----------
def case_card_press_anim() -> dict:
    base = temp_base()
    group = make_group(base, ["press_a.png", "press_b.png"], folder="g")
    workbench = make_workbench([group], base)
    card = workbench._cards[str(workbench._selected_path())]
    selected_rgb = rgb(QColor(config.REVIEW_CARD_SELECTED_BG))
    border_rgb = rgb(QColor(config.REVIEW_CARD_SELECTED_BORDER))

    class _LayoutSpy(QObject):
        def __init__(self) -> None:
            super().__init__()
            self.count = 0

        def eventFilter(self, obj, event) -> bool:  # noqa: N802
            if event.type() == QEvent.Type.LayoutRequest:
                self.count += 1
                self.sources.append(
                    (obj.__class__.__name__, obj.objectName())
                )
            return False

    spy = _LayoutSpy()
    spy.sources = []
    workbench.installEventFilter(spy)
    workbench.container.installEventFilter(spy)
    normal_rect = card.geometry()
    normal_bbox = color_bbox(card.grab(), selected_rgb)
    assert normal_bbox is not None, "未选中卡片没有选中底色像素"
    spy.count = 0
    spy.sources.clear()

    send_left(card, QEvent.Type.MouseButtonPress)
    assert pump(
        0.25,
        lambda: card.press_progress() >= 0.999
        and not card.is_press_animating(),
    ), (card.press_progress(), card.is_press_animating())
    pressed_bbox = color_bbox(card.grab(), selected_rgb)
    assert pressed_bbox is not None
    shrunk_w = (normal_bbox[2] - normal_bbox[0]) - (
        pressed_bbox[2] - pressed_bbox[0]
    )
    shrunk_h = (normal_bbox[3] - normal_bbox[1]) - (
        pressed_bbox[3] - pressed_bbox[1]
    )
    assert shrunk_w >= 1, (normal_bbox, pressed_bbox)
    assert shrunk_h >= 1, (normal_bbox, pressed_bbox)
    assert card.geometry() == normal_rect, (normal_rect, card.geometry())
    assert spy.count == 0, f"动画期间布局被请求 {spy.count} 次 {spy.sources}"
    # 按压不覆盖选中态边框颜色。
    pressed_border = color_bbox(card.grab(), border_rgb, tolerance=10)
    assert pressed_border is not None, "按压时选中边框颜色被覆盖"

    send_left(card, QEvent.Type.MouseButtonRelease)
    assert pump(0.25, lambda: not card.is_press_animating())
    assert card.press_progress() == 0.0
    released_bbox = color_bbox(card.grab(), selected_rgb)
    assert released_bbox == normal_bbox, (normal_bbox, released_bbox)
    assert card.geometry() == normal_rect
    assert spy.count == 0, f"回弹期间布局被请求 {spy.count} 次"

    baseline = len(card.findChildren(QAbstractAnimation))
    spy.count = 0
    spy.sources.clear()
    for _ in range(24):
        send_left(card, QEvent.Type.MouseButtonPress)
        send_left(card, QEvent.Type.MouseButtonRelease)
        app().processEvents()
    pump(0.35)
    assert card.press_progress() == 0.0
    assert not card.is_press_animating()
    assert spy.count == 0, f"连击期间布局被请求 {spy.count} 次 {spy.sources}"
    assert len(card.findChildren(QAbstractAnimation)) == baseline, (
        baseline,
        len(card.findChildren(QAbstractAnimation)),
    )

    workbench.close()
    release_events()
    return {
        "shrunk_w": int(shrunk_w),
        "shrunk_h": int(shrunk_h),
        "rect_delta": 0,
        "layout_requests": 0,
        "clicks": 24,
        "anim_baseline": baseline,
    }


# ---------- o) 单击=选中、双击=开大图，不互吞事件 ----------
def case_card_double_click() -> dict:
    base = temp_base()
    group = make_group(base, ["dbl_a.png", "dbl_b.png"], folder="g")
    workbench = make_workbench([group], base)
    members = group_members(group)
    default = pick_default_keeper(group)
    other = next(
        member for member in members if str(member.path) != str(default.path)
    )
    card = workbench._cards[str(other.path)]
    assert workbench._viewers == []

    click_card(card)
    pump(0.2)
    assert workbench._selected_path() == str(other.path)
    assert workbench._viewers == [], "单击误开了大图"

    send_left(card, QEvent.Type.MouseButtonPress)
    send_left(card, QEvent.Type.MouseButtonRelease)
    send_left(card, QEvent.Type.MouseButtonDblClick)
    send_left(card, QEvent.Type.MouseButtonRelease)
    pump(0.3)
    assert workbench._selected_path() == str(other.path), "双击把选中吞掉了"
    assert len(workbench._viewers) == 1, len(workbench._viewers)
    viewer = workbench._viewers[0]
    assert viewer._pixmap is not None and not viewer._pixmap.isNull()
    assert viewer._pixmap.width() <= max(1, other.width)
    assert viewer._pixmap.height() <= max(1, other.height)
    assert viewer.windowTitle().endswith(other.path.name), viewer.windowTitle()
    workbench.close()
    release_events()
    return {
        "single_selected": True,
        "single_no_viewer": True,
        "double_viewers": 1,
        "pixmap": [viewer._pixmap.width(), viewer._pixmap.height()],
    }


# ---------- p) 键盘可达：卡片可聚焦，空格/回车选中 ----------
def case_card_keyboard() -> dict:
    base = temp_base()
    group = make_group(base, ["key_a.png", "key_b.png"], folder="g")
    workbench = make_workbench([group], base)
    default = pick_default_keeper(group)
    other = next(
        member
        for member in group_members(group)
        if str(member.path) != str(default.path)
    )
    other_card = workbench._cards[str(other.path)]
    default_card = workbench._cards[str(default.path)]
    assert other_card.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert default_card.focusPolicy() == Qt.FocusPolicy.StrongFocus

    chain = [workbench.nextInFocusChain() for _ in range(64)]
    tabbed = any(card in chain for card in workbench._cards.values())
    tab_reachable = int(other_card.focusPolicy()) & int(
        Qt.FocusPolicy.TabFocus
    )
    assert tab_reachable, (other_card.focusPolicy(), tabbed)

    other_card.setFocus(Qt.FocusReason.TabFocusReason)
    pump(0.05)
    assert workbench.focusWidget() is other_card, workbench.focusWidget()
    QApplication.sendEvent(
        other_card,
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Space,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    pump(0.1)
    assert workbench._selected_path() == str(other.path)

    default_card.setFocus(Qt.FocusReason.TabFocusReason)
    pump(0.05)
    QApplication.sendEvent(
        default_card,
        QKeyEvent(
            QEvent.Type.KeyPress,
            Qt.Key.Key_Return,
            Qt.KeyboardModifier.NoModifier,
        ),
    )
    pump(0.1)
    assert workbench._selected_path() == str(default.path)
    focus_policy = int(other_card.focusPolicy())
    assert "QFrame#reviewCard:focus" in other_card.styleSheet()
    assert config.WORKBENCH_CARD_FOCUS_BORDER == config.ACCENT
    workbench.close()
    release_events()
    return {
        "focus_policy": focus_policy,
        "tab_reachable": bool(tab_reachable),
        "space_selected": Path(str(other.path)).name,
        "enter_selected": Path(str(default.path)).name,
        "focus_border": config.WORKBENCH_CARD_FOCUS_BORDER,
    }


# ---------- q) 紧凑卡片：长名省略、大小完整 ----------
def case_card_long_name() -> dict:
    import shutil

    root = Path(__file__).resolve().parent.parent
    base = Path(tempfile.mkdtemp(prefix="pfw-", dir=str(root)))
    long_name = "a" * 205 + ".png"
    group = make_group(base, [long_name, "short.png"], folder="")
    workbench = make_workbench([group], base)
    members = group_members(group)
    long_member = next(member for member in members if member.path.name == long_name)
    card = workbench._cards[str(long_member.path)]
    name = card.findChild(QLabel, "cardName")
    size = card.findChild(QLabel, "cardSize")
    assert name is not None and size is not None
    assert "…" in name.text(), name.text()[-12:]
    assert name.text() != long_name
    assert name.toolTip() == long_name
    assert size.text() == format_size(long_member.file_size), size.text()
    pad = workbench._px(config.WORKBENCH_CARD_PAD)
    available = config.REVIEW_CARD_WIDTH - 2 * pad
    assert name.fontMetrics().horizontalAdvance(name.text()) <= available, (
        name.fontMetrics().horizontalAdvance(name.text()),
        available,
    )
    tail = name.text()[-4:]
    size_text = size.text()
    diverges = name.text() != long_name
    assert card.layout().contentsMargins().left() == pad, (
        card.layout().contentsMargins().left(),
        pad,
    )
    workbench.close()
    release_events()
    shutil.rmtree(base, ignore_errors=True)
    return {
        "available": available,
        "name_tail": tail,
        "name_diverges": diverges,
        "size": size_text,
        "pad": pad,
    }


# ---------- r) 整组丢弃：确认文案 / 取消零移动 / 确认后 discard_all ----------
def case_discard_ui() -> dict:
    base = temp_base()
    group = make_group(base, ["d1.png", "d2.png", "d3.png"], folder="g0")
    other = make_group(base, ["e1.png", "e2.png"], folder="g1")
    contents = {member.path: member.path.read_bytes() for member in group_members(group)}
    service = make_service(base)
    calls: list[tuple[str, object]] = []

    def decide(g, action, keeper):
        calls.append((action, None if keeper is None else str(keeper.path)))
        return service.apply_decision(g, action, keeper)

    workbench = make_workbench([group, other], base, decide=decide)
    workbench.resize(1100, 720)
    pump(0.2)

    # 二次确认文案含真实张数（3 张），并明确提示“重复文件”。
    text = workbench._discard_confirm_text(group)
    assert "3张" in text, text
    assert TRASH_DIR_NAME in text, text

    # 取消：不产生任何决策调用与移动。
    workbench._ask_discard = lambda _group: False
    workbench.btn_discard.click()
    pump(0.1)
    assert calls == [], calls
    assert workbench._status[0] == STATUS_PENDING
    for member in group_members(group):
        assert member.path.is_file(), member.path
    assert not (base / TRASH_DIR_NAME / "g0" / "d1.png").exists()

    # 确认：以 discard_all + keeper=None 调用，全部移动后自动前进。
    workbench._ask_discard = lambda _group: True
    workbench.btn_discard.click()
    pump(0.15)
    assert calls == [("discard_all", None)], calls
    assert workbench._status[0] == STATUS_DISCARDED
    assert workbench.list_widget.currentRow() == 1
    assert "已处理（全部丢弃）" in workbench._list_items[0].text()
    assert workbench._summary["discarded"] == 1
    assert workbench._summary["moved"] == 3
    for member in group_members(group):
        assert not member.path.exists(), member.path
        destination = base / TRASH_DIR_NAME / member.rel_path
        assert destination.is_file(), destination
        assert destination.read_bytes() == contents[member.path]

    decision_calls = list(calls)
    workbench.close()  # 关闭后剩余组会挂起，额外产生 suspend 调用，不计入本用例断言。
    release_events()
    return {
        "confirm_text": text,
        "calls": decision_calls,
        "discarded": 1,
        "moved": 3,
        "advanced_row": 1,
        "status_text": "已处理（全部丢弃）",
    }


# ---------- s) 撤销本组：保留移动后恢复、可再次决策与再次撤销 ----------
def case_undo_group_confirm() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["ug1a.png", "ug1b.png"]),
        make_group(base, ["ug2a.png", "ug2b.png"]),
    ]
    service = make_service(base)
    workbench = make_workbench(
        groups,
        base,
        decide=lambda g, a, k: service.apply_decision(g, a, k),
        undo_group=lambda g: service.undo_group(g),
    )
    assert not workbench.btn_undo_group.isEnabled()

    workbench._confirm()
    pump(0.2)
    assert workbench._status[0] == STATUS_CONFIRMED
    assert (base / TRASH_DIR_NAME / "ug1b.png").is_file()
    assert not (base / "ug1b.png").exists()
    assert workbench.list_widget.currentRow() == 1
    # 自动前进到下一组待处理项：最近处理组的批次仍在，撤销按钮点亮并指向第 0 组。
    assert workbench.btn_undo_group.isEnabled()
    assert workbench._undo_target_index() == 0

    workbench._select_group(0)
    pump(0.2)
    assert workbench.btn_undo_group.isEnabled()
    assert workbench._undo_target_index() == 0
    assert workbench._group_undoable[0] is True

    workbench.btn_undo_group.click()
    pump(0.2)
    assert (base / "ug1b.png").is_file()
    assert not (base / TRASH_DIR_NAME).exists()
    assert workbench._status[0] == STATUS_PENDING
    assert "⏳待处理" in workbench._list_items[0].text()
    assert workbench.btn_confirm.isEnabled()
    assert workbench.btn_skip.isEnabled()
    assert not workbench.btn_undo_group.isEnabled()
    assert workbench._summary["confirmed"] == 0
    assert workbench._summary["moved"] == 0
    assert service.undo_stack == []

    # 重新决策：新的批次照常入栈，可再次撤销。
    workbench._confirm()
    pump(0.2)
    assert (base / TRASH_DIR_NAME / "ug1b.png").is_file()
    assert len(service.undo_stack) == 1
    workbench._select_group(0)
    pump(0.1)
    assert workbench.btn_undo_group.isEnabled()
    workbench.btn_undo_group.click()
    pump(0.2)
    assert (base / "ug1b.png").is_file()
    assert service.undo_stack == []
    assert workbench._status[0] == STATUS_PENDING

    status_after_undo = workbench._status[0]
    button_states = [
        workbench.btn_confirm.isEnabled(),
        workbench.btn_undo_group.isEnabled(),
    ]
    decision_calls = len(service.undo_stack)
    workbench.close()  # 关闭会把剩余待处理组挂起，返回值必须在关闭前快照。
    release_events()
    return {
        "status_after_undo": status_after_undo,
        "restored": True,
        "redecision_moved": True,
        "undo_stack_after_reundo": decision_calls,
        "button_states": button_states,
    }


# ---------- t) 撤销本组：整组丢弃后全部还原 ----------
def case_undo_group_discard() -> dict:
    base = temp_base()
    groups = [
        make_group(base, ["ud1.png", "ud2.png", "ud3.png"]),
        make_group(base, ["ue1.png", "ue2.png"]),
    ]
    contents = {
        member.path: member.path.read_bytes()
        for member in group_members(groups[0])
    }
    service = make_service(base)
    workbench = make_workbench(
        groups,
        base,
        decide=lambda g, a, k: service.apply_decision(g, a, k),
        undo_group=lambda g: service.undo_group(g),
    )
    workbench._ask_discard = lambda _group: True

    workbench.btn_discard.click()
    pump(0.2)
    assert workbench._status[0] == STATUS_DISCARDED
    assert "已处理（全部丢弃）" in workbench._list_items[0].text()
    assert workbench._summary["discarded"] == 1
    assert workbench._summary["moved"] == 3
    for member in group_members(groups[0]):
        assert not member.path.exists(), member.path
        assert (base / TRASH_DIR_NAME / member.rel_path).is_file()

    workbench._select_group(0)
    pump(0.2)
    assert workbench.btn_undo_group.isEnabled()
    workbench.btn_undo_group.click()
    pump(0.2)
    for member in group_members(groups[0]):
        assert member.path.is_file(), member.path
        assert member.path.read_bytes() == contents[member.path]
    assert not (base / TRASH_DIR_NAME).exists()
    assert workbench._status[0] == STATUS_PENDING
    assert "⏳待处理" in workbench._list_items[0].text()
    assert workbench._summary["discarded"] == 0
    assert workbench._summary["moved"] == 0
    assert workbench.btn_confirm.isEnabled()
    assert service.undo_stack == []

    status_after_undo = workbench._status[0]
    summary = dict(workbench._summary)
    workbench.close()  # 关闭会把剩余待处理组挂起，返回值必须在关闭前快照。
    release_events()
    return {
        "status_after_undo": status_after_undo,
        "all_restored": True,
        "trash_removed": True,
        "summary": summary,
    }


# ---------- u) 最后一组整组丢弃 → 空态撤销按钮点亮 → 点击回到对比界面 ----------
def case_undo_last_group() -> dict:
    base = temp_base()
    group = make_group(base, ["ul1.png", "ul2.png"])
    contents = {member.path: member.path.read_bytes() for member in group_members(group)}
    service = make_service(base)
    workbench = make_workbench(
        [group],
        base,
        decide=lambda g, a, k: service.apply_decision(g, a, k),
        undo_group=lambda g: service.undo_group(g),
    )
    workbench.resize(1100, 720)
    pump(0.2)
    workbench._ask_discard = lambda _group: True

    workbench.btn_discard.click()
    pump(0.2)
    # 只有一组：处理完立即进入空态，但撤销按钮必须仍然点亮。
    assert workbench._complete is True
    assert workbench.stack.currentWidget() is workbench.empty_page
    assert workbench._current_index == -1
    assert workbench._undo_target_index() == 0
    assert workbench.btn_undo_group.isEnabled()
    for member in group_members(group):
        assert not member.path.exists(), member.path

    workbench.btn_undo_group.click()
    pump(0.3)
    assert workbench._complete is False
    assert workbench.stack.currentWidget() is workbench.scroll
    assert workbench.list_widget.currentRow() == 0
    assert workbench.right_title.text().startswith("第 1/1 组")
    assert workbench._status[0] == STATUS_PENDING
    assert "⏳待处理" in workbench._list_items[0].text()
    assert workbench._selected_path() == str(group_members(group)[0].path)
    assert workbench.btn_confirm.isEnabled()
    assert workbench.btn_skip.isEnabled()
    assert not workbench.btn_undo_group.isEnabled()
    assert workbench._summary["discarded"] == 0
    assert workbench._summary["moved"] == 0
    for member in group_members(group):
        assert member.path.is_file(), member.path
        assert member.path.read_bytes() == contents[member.path]
    assert service.undo_stack == []

    summary = dict(workbench._summary)
    undo_after = workbench.btn_undo_group.isEnabled()
    complete_after = workbench._complete
    status_after = workbench._status[0]
    right_is_scroll = workbench.stack.currentWidget() is workbench.scroll
    list_row = workbench.list_widget.currentRow()
    workbench.close()  # 关闭会把剩余待处理组挂起，返回值必须在关闭前快照。
    release_events()
    return {
        "button_before": True,
        "status_after": status_after,
        "right_is_scroll": right_is_scroll,
        "list_row": list_row,
        "complete_after": complete_after,
        "undo_after": undo_after,
        "summary": summary,
    }


# ---------- v) 底部提示与按钮彻底分开 + 超长文本压力 ----------
def case_footer_stress() -> dict:
    base = temp_base()
    group = make_group(base, ["fs1.png", "fs2.png"])
    workbench = make_workbench([group], base)

    hint = workbench.footer_hint
    footer = workbench.footer_layout
    button_row = workbench.button_row
    buttons = (
        workbench.btn_skip,
        workbench.btn_undo_group,
        workbench.btn_discard,
        workbench.btn_confirm,
    )
    # 结构：提示是 QVBoxLayout 的第 0 项（独占一行），按钮行是第 1 项，
    # 提示绝不出现在按钮的水平布局里。
    assert footer.itemAt(0).widget() is hint
    assert footer.itemAt(1).layout() is button_row
    assert button_row.indexOf(hint) == -1
    assert hint.wordWrap() is True

    workbench.resize(900, 600)
    pump(0.2)
    hint.setText("提示压力测试：" + "本行文字需要自动换行且绝不遮挡按钮。" * 60)
    pump(0.3)

    hint_bottom = hint.mapTo(workbench, QPoint(0, hint.height())).y()
    button_top = workbench.btn_skip.mapTo(workbench, QPoint(0, 0)).y()
    assert hint_bottom <= button_top, (hint_bottom, button_top)
    line_height = hint.fontMetrics().height()
    assert hint.height() > line_height, (hint.height(), line_height)

    button_min = workbench._px(config.WORKBENCH_BUTTON_MIN_WIDTH)
    button_gap = workbench._px(config.WORKBENCH_BUTTON_GAP)
    row_sum = sum(button.width() for button in buttons) + button_gap * 3
    # 可用宽度足够容纳 4 个最小按钮 + 3 个间距；按钮本身也各自不小于最小宽。
    assert row_sum >= button_min * 4 + button_gap * 3, row_sum
    for button in buttons:
        assert button.width() >= button_min, (button.text(), button.width())
        assert (
            button.mapTo(workbench, QPoint(0, 0)).y()
            == workbench.btn_skip.mapTo(workbench, QPoint(0, 0)).y()
        )

    # 再把分界线拖到极端，验证文字增高不会反过来挤压按钮。
    workbench.splitter.setSizes([10000, 0])
    pump(0.2)
    hint_bottom = hint.mapTo(workbench, QPoint(0, hint.height())).y()
    button_top = workbench.btn_skip.mapTo(workbench, QPoint(0, 0)).y()
    assert hint_bottom <= button_top, (hint_bottom, button_top)
    for button in buttons:
        assert button.width() >= button_min, (button.text(), button.width())

    workbench.close()
    release_events()
    return {
        "separate_layouts": True,
        "word_wrap": hint.wordWrap(),
        "hint_height": hint.height(),
        "line_height": line_height,
        "button_widths": [button.width() for button in buttons],
        "button_min": button_min,
        "button_gap": button_gap,
        "row_min": button_min * 4 + button_gap * 3,
        "no_overlap": hint_bottom <= button_top,
    }


CASES = {
    "layout": case_layout,
    "list_text": case_list_text,
    "discard_ui": case_discard_ui,
    "undo_group_confirm": case_undo_group_confirm,
    "undo_group_discard": case_undo_group_discard,
    "undo_last_group": case_undo_last_group,
    "footer_stress": case_footer_stress,
    "styles": case_styles,
    "advance": case_advance,
    "switch": case_switch,
    "memory": case_memory,
    "decisions": case_decisions,
    "service_batch": case_service_batch,
    "empty_cleanup": case_empty_cleanup,
    "regression": case_regression,
    "ui_polish": case_ui_polish,
    "card_tree": case_card_tree,
    "card_selection": case_card_selection,
    "card_press_anim": case_card_press_anim,
    "card_double_click": case_card_double_click,
    "card_keyboard": case_card_keyboard,
    "card_long_name": case_card_long_name,
}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "layout"
    if name not in CASES:
        raise SystemExit(f"unknown case: {name}")
    app()
    payload = CASES[name]()
    payload["ok"] = True
    payload["case"] = name
    print(json.dumps(payload, ensure_ascii=False))
    app().processEvents()


if __name__ == "__main__":
    main()
