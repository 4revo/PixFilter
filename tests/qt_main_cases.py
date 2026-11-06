"""Qt 主界面的独立子进程用例（由 test_ui_qt.py 调用）。

运行：QT_QPA_PLATFORM=offscreen python tests/qt_main_cases.py <case>
每个用例失败即抛异常并以非零码退出；成功打印 ``OK <case>``。
"""

from __future__ import annotations

import os
import math
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image  # noqa: E402
from PySide6.QtCore import (  # noqa: E402
    QEvent,
    QMargins,
    QPoint,
    QPointF,
    Qt,
    QTimer,
    QThread,
)
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QFontMetrics,
    QGuiApplication,
    QMouseEvent,
)
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractItemView,
    QApplication,
    QFrame,
    QTextBrowser,
)

import config  # noqa: E402
import pixfilter_core  # noqa: E402
import review_qt  # noqa: E402
import ui_qt_main  # noqa: E402
from review_workbench_qt import QtReviewWorkbench  # noqa: E402
from pixfilter_core import (  # noqa: E402
    DuplicateGroup,
    ImageInfo,
    TRASH_DIR_NAME,
    format_size,
    pick_default_keeper,
)

APP = None


def app() -> QApplication:
    global APP
    APP = ui_qt_main.ensure_qapplication()
    # 硬性要求：所有 UI 用例只允许跑在 offscreen 子进程里。
    assert QGuiApplication.platformName() == "offscreen", (
        QGuiApplication.platformName()
    )
    return APP


def show_offscreen(widget) -> None:
    """离屏显示：即使平台插件非预期，也不会投到真实桌面。"""
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    widget.show()


def send_mouse(widget, kind, pos=None) -> None:
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


def make_window() -> ui_qt_main.PixFilterQtWindow:
    window = ui_qt_main.PixFilterQtWindow()
    show_offscreen(window)
    pump(0.3)
    return window


def make_png(path: Path, color=(200, 30, 30), size=(64, 64)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    return path


def make_pattern(path: Path) -> Path:
    """强结构图形，确保它与纯色图 pHash 距离远大于阈值 8。"""
    from PIL import ImageDraw

    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (96, 96), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse([6, 6, 60, 60], fill=(0, 0, 0))
    draw.rectangle([70, 10, 90, 90], fill=(0, 0, 0))
    image.save(path)
    return path


def make_group(base: Path, names: list[str], kind: str = "exact") -> DuplicateGroup:
    infos = []
    for index, name in enumerate(names):
        path = base / name
        if not path.exists():
            make_png(path, color=(200, 30 + index * 10, 30))
        infos.append(
            ImageInfo(
                path=path,
                rel_path=Path(name),
                file_size=path.stat().st_size,
                width=64,
                height=64,
            )
        )
    return DuplicateGroup(kind=kind, keeper=infos[0], duplicates=infos[1:])


def make_temp_base() -> Path:
    return Path(tempfile.mkdtemp(prefix="pixfilter-qt-"))


def add_pending(window, group, index: int, total: int, base: Path):
    """登记挂起组并直接通知控制器（该用例本身就在主线程）。"""
    suspended = window._service.add_suspended(group, index, total, base)
    window.controller.on_suspended(suspended)
    return suspended


def select_pending_row(window, row: int) -> None:
    """单击选中某行（单选模式），并等待选择信号回流控制器。"""
    window.pending_table.selectRow(row)
    pump(0.05)


def abandon_all_pending(window) -> None:
    """单选模式下逐行放弃，直到列表清空。"""
    for _ in range(64):
        if window.pending_table.rowCount() == 0:
            return
        select_pending_row(window, 0)
        window.controller.on_abandon_pending()
        pump(0.05)
    raise AssertionError("放弃处理未清空待处理列表")


def open_workbench(window) -> object:
    """等待工作台创建完成并返回实例。"""
    window.controller.on_resume_pending()
    pump(0.3)
    workbench = window._workbench
    assert workbench is not None, "工作台未创建"
    return workbench


def focus_row_workbench(window, row: int) -> object:
    """模拟双击某行：工作台含全部待处理组，并聚焦该行对应的组。"""
    item = window.pending_table.item(row, 0)
    assert item is not None
    key = item.data(Qt.ItemDataRole.UserRole)
    window.controller.on_resume_pending(int(key))
    pump(0.3)
    workbench = window._workbench
    assert workbench is not None, "工作台未创建"
    return workbench


def case_a() -> None:
    window = make_window()
    checks = {
        "btn_choose_folder": ("点击此处选择扫描文件夹", True),
        "btn_undo": (config.UNDO_ALL_TEXT, False),
        "btn_open_trash": ("打开重复目录", True),
        "btn_start": ("开始扫描", True),
        "btn_stop": ("停止", False),
        "btn_pending_continue": ("继续处理", False),
        "btn_pending_abandon": ("放弃处理选中组", False),
        "btn_pending_abandon_all": ("全部放弃", False),
        "btn_github": ("GitHub 项目地址", True),
    }
    for name, (text, enabled) in checks.items():
        button = getattr(window, name)
        assert button.text() == text, (name, button.text())
        assert button.isEnabled() == enabled, (name, button.isEnabled())
        assert button.cursor().shape() == (
            Qt.CursorShape.PointingHandCursor if enabled else Qt.CursorShape.ArrowCursor
        ), name
    slider = window.threshold_slider
    assert (slider.minimum(), slider.maximum(), slider.value()) == (1, 20, 8)
    assert window.threshold_value_label.text() == "当前: 8"
    assert "pHash 8x8" in window.threshold_help_label.text()
    assert "默认 8" in window.threshold_help_label.text()
    assert "越低越严格" in window.threshold_help_label.text()
    assert isinstance(window.log_view, QTextBrowser)
    assert "暂无待处理组" in window.pending_empty.text()
    assert window.pending_stack.currentWidget() is window.pending_empty
    assert window.status_label.text().startswith("就绪")
    assert window.progress.isHidden()
    assert window.btn_github.parentWidget() is window.centralWidget()
    assert window.btn_github.text() == "GitHub 项目地址"
    assert window.hint_label.text() == window.hint_text

    # 顶部一行：同一 y、10px 间距。
    row = window.folder_row
    buttons = [window.btn_choose_folder, window.btn_undo, window.btn_open_trash]
    tops = {button.mapTo(window, QPoint(0, 0)).y() for button in buttons}
    assert len(tops) == 1, tops
    positions = sorted(
        button.mapTo(window, QPoint(0, 0)).x() for button in buttons
    )
    widths = {button.mapTo(window, QPoint(0, 0)).x(): button.width() for button in buttons}
    gaps = [
        positions[i + 1] - (positions[i] + widths[positions[i]])
        for i in range(len(positions) - 1)
    ]
    assert row.spacing() == config.BUTTON_GAP
    assert all(8 <= gap <= 12 for gap in gaps), gaps
    assert window.minimumWidth() == config.MIN_WINDOW_WIDTH
    assert window.minimumHeight() == config.MIN_WINDOW_HEIGHT

    # 圆角按钮 QSS：圆角、hover、pressed 下沉、disabled。
    for button in (window.btn_start, window.btn_undo):
        sheet = button.styleSheet()
        assert "border-radius: 8px" in sheet or "border-radius: 6px" in sheet
        for token in (":hover", ":pressed", ":disabled", "padding-top:"):
            assert token in sheet, token
    stop_clicks = {"count": 0}
    window.btn_stop.clicked.connect(
        lambda: stop_clicks.__setitem__("count", stop_clicks["count"] + 1)
    )
    QTest.mouseClick(window.btn_stop, Qt.MouseButton.LeftButton)
    assert stop_clicks["count"] == 0
    assert window.btn_undo.cursor().shape() == Qt.CursorShape.ArrowCursor
    assert window.btn_start.cursor().shape() == Qt.CursorShape.PointingHandCursor

    icon = ui_qt_main.load_app_icon()
    assert not icon.isNull()
    for size in (16, 32, 48, 256):
        pixmap = icon.pixmap(size, size)
        assert not pixmap.isNull(), size
    with mock.patch.object(
        ui_qt_main.sys, "_MEIPASS", os.path.abspath("fake_meipass"), create=True
    ):
        resolved = ui_qt_main.resource_path(config.ICON_FILE_NAME)
        assert "fake_meipass" in resolved
    assert not window.windowIcon().isNull()
    window.close()
    pump(0.1)
    print("OK a")


def case_b() -> None:
    window = make_window()
    base = make_temp_base()
    window._service.folder = base
    captured: list[int] = []

    def fake_start(folder, threshold, max_workers=None):
        captured.append(threshold)
        assert isinstance(threshold, int)
        assert Path(folder) == base
        return False

    window._service.start_scan = fake_start  # type: ignore[method-assign]
    for value in (1, 8, 20):
        window.threshold_slider.setValue(value)
        pump(0.05)
        assert window.threshold_value_label.text() == f"当前: {value}"
        assert window.controller.get_threshold() == value
        window.controller.on_start_clicked()
        pump(0.05)
        assert captured[-1] == value, captured
    assert captured == [1, 8, 20]
    window.close()
    pump(0.1)
    print("OK b")


def case_c() -> None:
    window = make_window()
    base = make_temp_base()
    window.append_copyable_path(base)
    block = list(window.log_view._link_blocks)[-1]
    cursor = window.log_view.document().findBlockByNumber(block)
    from PySide6.QtGui import QTextCursor

    pos = window.log_view.cursorRect(QTextCursor(cursor)).center()
    QTest.mouseClick(window.log_view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    pump(0.1)
    assert app().clipboard().text() == str(base), app().clipboard().text()
    lines = window.log_view.toPlainText().splitlines()
    assert lines[-1] == "✅ 路径已复制到剪贴板", lines[-1]
    assert "当前扫描目录" in lines[-2]
    assert str(base) in lines[-2]

    window.append_log("skip", "⚠️ 跳过无法读取的图片：broken.jpg")
    assert "⚠️ 跳过无法读取的图片：broken.jpg" in window.log_view.toPlainText()
    fmt_block = window.log_view.document().lastBlock()
    assert "跳过无法读取" in fmt_block.text()
    frag = fmt_block.begin().fragment()
    assert frag.isValid()
    assert frag.charFormat().foreground().color().name().lower() == config.LOG_SKIP_FG.lower()

    # 非链接行点击不复制。
    QTest.mouseClick(
        window.log_view.viewport(),
        Qt.MouseButton.LeftButton,
        pos=window.log_view.cursorRect(QTextCursor(fmt_block)).center(),
    )
    pump(0.05)
    assert app().clipboard().text() == str(base)
    window.close()
    pump(0.1)
    print("OK c")


def case_d() -> None:
    window = make_window()
    base = make_temp_base()
    group1 = make_group(base, ["a.png", "a_copy.png"])
    group2 = make_group(base, ["b.png", "b_copy.png"])
    group3 = make_group(base, ["c.png", "c_copy.png"])

    for index, group in enumerate((group1, group2, group3), 1):
        add_pending(window, group, index, 3, base)
    pump(0.1)
    assert window.pending_table.rowCount() == 3
    assert window.pending_stack.currentWidget() is window.pending_table
    assert window.pending_count_label.text() == "共 3 组"
    assert window.btn_pending_continue.isEnabled()

    # 1) confirm：双击首行 → 工作台聚焦该组 → 确认移动，行移除。
    workbench = focus_row_workbench(window, 0)
    assert workbench.current_index == 0
    workbench._decide_current("confirm")
    pump(0.3)
    moved = base / TRASH_DIR_NAME / "a_copy.png"
    assert moved.is_file(), moved
    assert (base / "a.png").is_file()
    assert window.pending_table.rowCount() == 2

    # 2) suspend：X 关闭工作台，未处理组留在列表且零移动。
    workbench.close()
    pump(0.4)
    assert window._workbench is None
    assert window._service.has_undo
    assert window.btn_undo.isEnabled()
    assert window.pending_table.rowCount() == 2
    assert not (base / TRASH_DIR_NAME / "b_copy.png").exists()
    assert window.controller.pending_busy is False
    rows = [window.pending_table.item(i, 4).text() for i in range(2)]
    assert all(text == "待处理" for text in rows), rows

    # 3) skip：聚焦该组后跳过，零移动并移除。
    workbench = focus_row_workbench(window, 0)
    workbench._decide_current("skip")
    pump(0.3)
    assert window.pending_table.rowCount() == 1
    assert not (base / TRASH_DIR_NAME / "b_copy.png").exists()
    workbench.close()
    pump(0.4)

    # 4) 放弃处理按钮：零移动并记录。
    before = sorted(p.name for p in (base / TRASH_DIR_NAME).rglob("*") if p.is_file())
    abandon_all_pending(window)
    after = sorted(p.name for p in (base / TRASH_DIR_NAME).rglob("*") if p.is_file())
    assert before == after
    assert window.pending_table.rowCount() == 0
    assert window.pending_stack.currentWidget() is window.pending_empty
    assert "[放弃处理]" in window.log_view.toPlainText()
    assert not (base / TRASH_DIR_NAME / "c_copy.png").exists()
    assert window.btn_pending_continue.isEnabled() is False
    assert window.btn_pending_abandon.isEnabled() is False
    window.close()
    pump(0.1)
    print("OK d")


def case_e() -> None:
    base = make_temp_base()
    group = make_group(base, ["e1.png", "e2.png"])
    missing = base / "missing.png"
    group = DuplicateGroup(
        kind=group.kind,
        keeper=group.keeper,
        duplicates=group.duplicates
        + [ImageInfo(path=missing, rel_path=Path("missing.png"), file_size=0)],
    )
    logs: list[tuple[str, str]] = []
    dialog = review_qt.QtReviewDialog(
        group, 1, 1, log=lambda message, kind="info": logs.append((message, kind))
    )
    show_offscreen(dialog)
    pump(0.3)
    cards = dialog.findChildren(QFrame, "reviewCard")
    assert len(cards) == 3, len(cards)
    for card in cards:
        assert card.width() == config.REVIEW_CARD_WIDTH
        assert card.height() == config.REVIEW_CARD_HEIGHT
    assert len(dialog._thumbnail_labels) == 2
    for thumb in dialog._thumbnail_labels + dialog._placeholders:
        assert thumb.width() == config.THUMB_MAX
        assert thumb.height() == config.THUMB_MAX
    for pixmap in dialog._pixmaps:
        assert not pixmap.isNull()
        assert pixmap.width() <= config.THUMB_MAX
        assert pixmap.height() <= config.THUMB_MAX
    assert dialog._selected_path() == str(pick_default_keeper(group).path)
    assert len(dialog._placeholders) == 1
    assert any("missing.png" in message and "无法加载图片" in message for message, _ in logs)

    # 大图：屏幕 80% 内、KeepAspectRatio；空格关闭。
    dialog._open_viewer(group.keeper)
    pump(0.2)
    viewer = dialog._viewers[-1]
    screen = QGuiApplication.primaryScreen().availableGeometry()
    assert viewer.width() <= int(screen.width() * config.VIEWER_SCREEN_RATIO) + 24
    assert viewer.height() <= int(screen.height() * config.VIEWER_SCREEN_RATIO) + 24
    if viewer._pixmap is not None:
        assert viewer._pixmap.width() <= int(screen.width() * config.VIEWER_SCREEN_RATIO)
        assert viewer._pixmap.height() <= int(screen.height() * config.VIEWER_SCREEN_RATIO)
    QTest.keyClick(viewer, Qt.Key.Key_Space)
    pump(0.1)
    assert not viewer.isVisible()

    # 三种决策映射。
    dialog._finish("confirm")
    assert dialog.decision is not None
    assert dialog.decision[0] == "confirm"
    assert dialog.decision[1].path == dialog.default.path
    second = review_qt.QtReviewDialog(group, 1, 1)
    show_offscreen(second)
    pump(0.1)
    second._finish("skip")
    assert second.decision[0] == "skip"
    third = review_qt.QtReviewDialog(group, 1, 1)
    show_offscreen(third)
    pump(0.1)
    third.close()
    pump(0.1)
    assert third.decision[0] == "suspend"
    for item in (dialog, second, third):
        item.close()
    pump(0.1)
    print("OK e")


def case_f() -> None:
    window = make_window()
    heartbeat = {"count": 0}

    timer = QTimer()
    timer.setInterval(10)
    timer.timeout.connect(lambda: heartbeat.__setitem__("count", heartbeat["count"] + 1))
    timer.start()

    window.show_toast("测试 Toast")
    toast = window._toast
    assert toast is not None
    start = toast.pos()
    end = toast._end_pos
    win_geo = window.geometry()
    assert start.y() < win_geo.y(), (start, win_geo)
    assert abs(start.x() - (win_geo.x() + (win_geo.width() - toast.width()) // 2)) <= 1
    assert toast.windowOpacity() == config.QT_TOAST_START_FACTOR

    pump(0.25)
    assert toast.pos() == end, (toast.pos(), end)
    assert win_geo.contains(toast.geometry()), (win_geo, toast.geometry())

    pump(config.TOAST_HOLD_MS / 1000.0 + 0.2)
    assert toast.windowOpacity() < 1.0, toast.windowOpacity()
    mid = heartbeat["count"]
    pump(config.QT_TOAST_FADE_MS / 1000.0 + 0.3)
    assert toast.windowOpacity() <= 0.05, toast.windowOpacity()
    assert not toast.isVisible()
    assert heartbeat["count"] - mid > 10, heartbeat
    assert heartbeat["count"] > 150, heartbeat
    assert win_geo.contains(toast.geometry()), (win_geo, toast.geometry())
    timer.stop()
    window.close()
    pump(0.1)
    print("OK f")


def case_toast_round() -> None:
    """Toast 圆角/透明：结构断言 + 逐像素真实渲染断言。"""

    def greenish(color) -> bool:
        return (
            color.alpha() > 160
            and color.green() >= 120
            and color.red() <= 130
            and color.blue() <= 140
        )

    window = ui_qt_main.PixFilterQtWindow()
    # 全程 offscreen + WA_DontShowOnScreen：不出现真实窗口，也不移动光标。
    show_offscreen(window)
    pump(0.3)

    text = "扫描完成，已处理 12 个文件"
    window.show_toast(text)
    toast = window._toast
    assert toast is not None
    assert toast.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert not toast.autoFillBackground()
    root_qss = toast.styleSheet()
    assert "QWidget#toastRoot" in root_qss, root_qss
    assert "transparent" in root_qss, root_qss
    assert "background-color" not in root_qss, root_qss

    frame = toast._frame
    assert frame.objectName() == "toastFrame"
    frame_qss = frame.styleSheet()
    assert "QFrame#toastFrame" in frame_qss, frame_qss
    assert "border: none" in frame_qss, frame_qss
    assert f"border-radius: {config.TOAST_RADIUS}px" in frame_qss, frame_qss
    assert config.TOAST_FILL.lower() in frame_qss.lower(), frame_qss
    assert "QFrame {" not in frame_qss, frame_qss

    label_qss = toast._label.styleSheet()
    assert "background: transparent" in label_qss, label_qss
    assert "border: none" in label_qss, label_qss

    margins = toast.layout().contentsMargins()
    assert margins == QMargins(0, 0, 0, 0), margins
    assert toast.layout().spacing() == 0
    assert frame.geometry() == toast.rect(), (frame.geometry(), toast.rect())
    assert toast._start_pos.y() < toast._end_pos.y()

    pump(0.35, lambda: toast.pos() == toast._end_pos)
    assert toast.pos() == toast._end_pos, (toast.pos(), toast._end_pos)
    assert toast.windowOpacity() >= config.QT_TOAST_START_FACTOR - 0.02

    geo = window.geometry()
    dx = toast.x() - (geo.x() + (geo.width() - toast.width()) // 2)
    dy = toast.y() - geo.y()
    assert abs(dx) <= 1, dx
    assert dy == config.QT_TOAST_TOP_PAD, (dy, config.QT_TOAST_TOP_PAD)

    pixmap = toast.grab()
    ratio = pixmap.devicePixelRatio() or 1.0
    image = pixmap.toImage()
    width, height = image.width(), image.height()
    target = (0x4C, 0xAF, 0x50)

    corners = [
        image.pixelColor(0, 0),
        image.pixelColor(width - 1, 0),
        image.pixelColor(0, height - 1),
        image.pixelColor(width - 1, height - 1),
    ]
    for color in corners:
        assert color.alpha() <= 20, [c.getRgb() for c in corners]
        assert not greenish(color), [c.getRgb() for c in corners]

    counts: dict[tuple[int, int, int], int] = {}
    for y in range(height):
        for x in range(width):
            color = image.pixelColor(x, y)
            if color.alpha() > 250:
                key = (color.red(), color.green(), color.blue())
                counts[key] = counts.get(key, 0) + 1
    assert counts, "Toast 内部没有任何不透明像素"
    dominant = max(counts, key=counts.get)
    assert dominant == target, dominant

    mid_y = height // 2
    pads = [
        image.pixelColor(6, mid_y),
        image.pixelColor(width - 7, mid_y),
        image.pixelColor(width // 2, 4),
    ]
    for color in pads:
        assert color.getRgb() == target + (255,), color.getRgb()

    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        for x in range(width):
            if greenish(image.pixelColor(x, y)):
                xs.append(x)
                ys.append(y)
    assert xs and ys
    assert min(xs) >= 0 and max(xs) <= width - 1
    assert min(ys) >= 0 and max(ys) <= height - 1

    radius = config.TOAST_RADIUS
    top_inset = next(
        (x for x in range(width) if greenish(image.pixelColor(x, 0))), width
    ) / ratio
    expected_top = radius - math.sqrt(radius ** 2 - (radius - 0.5) ** 2)
    assert abs(top_inset - expected_top) <= 2.0, (top_inset, expected_top)

    diagonal = next(
        (k for k in range(min(width, height)) if greenish(image.pixelColor(k, k))),
        -1,
    )
    assert diagonal >= 0, "圆角对角线方向没有绿色像素"
    diagonal_logical = diagonal / ratio
    expected_diag = radius - radius / math.sqrt(2)
    assert abs(diagonal_logical - expected_diag) <= 1.5, (
        diagonal_logical,
        expected_diag,
    )

    # 长文案：宽度封顶 + 省略；短/中文案：不低于 min_width 且不超过文字宽 + padding。
    metrics = QFontMetrics(toast._label.font())
    long_text = (
        "这是一条非常长的提示文案，用来确认 Toast 宽度不会超过上限并且文字会被省略。"
        "这是第二条用于加长的句子。这是第三条用于加长的句子。"
    )
    window.show_toast(long_text)
    long_toast = window._toast
    assert long_toast is not None
    long_width = long_toast.width()
    assert long_width <= config.TOAST_MAX_WIDTH, (long_width, config.TOAST_MAX_WIDTH)
    assert long_width > config.TOAST_MAX_WIDTH - 2 * config.TOAST_PAD_X, long_width
    assert long_toast._label.text() != long_text
    assert long_toast._label.text().endswith("…"), long_toast._label.text()
    assert (
        metrics.horizontalAdvance(long_toast._label.text())
        <= config.TOAST_MAX_WIDTH - 2 * config.TOAST_PAD_X
    )

    window.show_toast("已复制")
    assert window._toast.width() == config.TOAST_MIN_WIDTH, window._toast.width()

    medium_text = "已复制到剪贴板，可在浏览器粘贴"
    window.show_toast(medium_text)
    medium_toast = window._toast
    medium_width = medium_toast.width()
    advance = metrics.horizontalAdvance(medium_text)
    assert medium_toast._label.text() == medium_text
    assert medium_width >= config.TOAST_MIN_WIDTH, medium_width
    assert medium_width <= advance + 2 * config.TOAST_PAD_X, (medium_width, advance)
    assert medium_width <= config.TOAST_MAX_WIDTH

    # 动画：滑入到位 → 停留 → 淡出 → 不可见。
    pump(0.35)
    assert medium_toast.pos() == medium_toast._end_pos
    assert medium_toast.windowOpacity() >= 0.98
    assert medium_toast._hold.isActive(), "停留定时器未启动"
    pump(config.TOAST_HOLD_MS / 1000.0 + 0.15)
    fading = medium_toast.windowOpacity()
    assert fading < 0.99, fading
    pump(config.QT_TOAST_FADE_MS / 1000.0 + 0.3)
    assert medium_toast.windowOpacity() <= 0.05, medium_toast.windowOpacity()
    assert not medium_toast.isVisible()

    print(
        f"toast platform={QGuiApplication.platformName()!r} dx={dx} dy={dy}"
        f" corners={[c.getRgb() for c in corners]}"
        f" dominant={dominant}"
        f" top_inset={top_inset:.2f}px diag_inset={diagonal_logical:.2f}px"
        f" long_width={long_width} medium_width={medium_width}"
        f" fade_mid={fading:.2f}"
    )
    window.close()
    pump(0.1)
    print("OK toast_round")


def _color_bbox(pixmap, rect, target: QColor, tolerance: int = 8):
    """窗口渲染结果中匹配目标色的像素包围盒（返回逻辑坐标）。"""
    image = pixmap.toImage()
    ratio = pixmap.devicePixelRatio() or 1.0
    left, top, width, height = rect
    xs: list[int] = []
    ys: list[int] = []
    for y in range(top, top + height):
        for x in range(left, left + width):
            px = int(round(x * ratio))
            py = int(round(y * ratio))
            if not (0 <= px < image.width() and 0 <= py < image.height()):
                continue
            color = image.pixelColor(px, py)
            if color.alpha() < 200:
                continue
            if (
                abs(color.red() - target.red()) <= tolerance
                and abs(color.green() - target.green()) <= tolerance
                and abs(color.blue() - target.blue()) <= tolerance
            ):
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _sample_cell(pixmap, rect) -> QColor:
    image = pixmap.toImage()
    ratio = pixmap.devicePixelRatio() or 1.0
    x = rect.left() + 2
    y = rect.top() + 2
    return image.pixelColor(int(round(x * ratio)), int(round(y * ratio)))


def _rgb(color: QColor) -> tuple[int, int, int]:
    return (color.red(), color.green(), color.blue())


def _max_channel_delta(colors) -> int:
    return max(
        max(abs(a - b) for a, b in zip(left, right))
        for left in colors
        for right in colors
    )


def case_press_bounce() -> None:
    """按钮按压回弹：paintEvent 缩放、rect 恒定、无布局请求、连击无残留。

    全程 offscreen：合成鼠标事件直接投递给控件，不移动系统光标。
    """
    from PySide6.QtCore import QAbstractAnimation, QEvent, QObject, QPointF
    from PySide6.QtGui import QColor, QMouseEvent
    from PySide6.QtWidgets import QApplication

    window = ui_qt_main.PixFilterQtWindow()
    show_offscreen(window)
    pump(0.4)

    button = ui_qt_main.QtButton("按压动画测试", primary=True)
    window.centralWidget().layout().addWidget(button)
    disabled = ui_qt_main.QtButton("禁用态")
    disabled.setEnabled(False)
    window.centralWidget().layout().addWidget(disabled)
    pump(0.3)

    class _LayoutSpy(QObject):
        def __init__(self) -> None:
            super().__init__()
            self.count = 0

        def eventFilter(self, obj, event) -> bool:  # noqa: N802
            if event.type() == QEvent.Type.LayoutRequest:
                self.count += 1
            return False

    spy = _LayoutSpy()
    button.parentWidget().installEventFilter(spy)
    window.installEventFilter(spy)

    def snapshot(target: str):
        origin = button.mapTo(window, QPoint(0, 0))
        rect = (origin.x(), origin.y(), button.width(), button.height())
        bbox = _color_bbox(window.grab(), rect, QColor(target))
        assert bbox is not None, f"{target} 按钮背景没有任何像素"
        return bbox, rect

    centre = QPoint(button.width() // 2, button.height() // 2)
    normal_bbox, region = snapshot(config.ACCENT)
    normal_rect = button.geometry()

    def send_mouse_to(widget, kind, point=None) -> None:
        if point is None:
            point = widget.rect().center()
        button_type = Qt.MouseButton.LeftButton
        mouse_event = QMouseEvent(
            kind,
            QPointF(point),
            QPointF(point),
            button_type,
            button_type,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(widget, mouse_event)

    def send_mouse(kind) -> None:
        send_mouse_to(button, kind, centre)

    send_mouse(QEvent.Type.MouseButtonPress)
    pump(0.2)
    assert button.press_progress() >= 0.999, button.press_progress()
    assert not button.is_press_animating()
    origin = button.mapTo(window, QPoint(0, 0))
    pressed_shot = window.grab()
    ratio = pressed_shot.devicePixelRatio() or 1.0
    pressed_image = pressed_shot.toImage()
    # 从按钮内部取真实绘制色，避免把文字像素当作背景色。
    sample_x = int(round((origin.x() + 12) * ratio))
    sample_y = int(round((origin.y() + button.height() // 2) * ratio))
    pressed_target = pressed_image.pixelColor(sample_x, sample_y)
    pressed_bbox = _color_bbox(
        pressed_shot,
        (origin.x(), origin.y(), button.width(), button.height()),
        pressed_target,
    )
    assert pressed_bbox is not None, "按下态按钮背景没有任何像素"
    pressed_rect = button.geometry()

    # 缩放后的外圈必须由父背景覆盖，不能残留旧的按钮像素。
    outer = pressed_image.pixelColor(
        int(round((origin.x() + button.width() // 2) * ratio)),
        int(round(origin.y() * ratio)),
    )
    assert outer.alpha() < 40 or _rgb(outer) != _rgb(pressed_target), (
        "按压缩放后外圈残留旧底色",
        _rgb(outer),
    )

    send_mouse(QEvent.Type.MouseButtonRelease)
    pump(0.25)
    assert button.press_progress() == 0.0
    assert not button.is_press_animating()
    released_bbox, region = snapshot(config.ACCENT)
    released_rect = button.geometry()

    shrank_w = (normal_bbox[2] - normal_bbox[0]) - (pressed_bbox[2] - pressed_bbox[0])
    shrank_h = (normal_bbox[3] - normal_bbox[1]) - (pressed_bbox[3] - pressed_bbox[1])
    assert shrank_w >= 1, (normal_bbox, pressed_bbox)
    assert shrank_h >= 1, (normal_bbox, pressed_bbox)
    assert _rgb(pressed_target) in (
        _rgb(QColor(config.ACCENT_PRESS)),
        _rgb(QColor(config.ACCENT)),
    ), _rgb(pressed_target)
    assert released_bbox == normal_bbox, (normal_bbox, released_bbox)
    assert normal_rect == pressed_rect == released_rect, (
        normal_rect, pressed_rect, released_rect,
    )
    assert spy.count == 0, f"动画期间布局被请求 {spy.count} 次"

    # disabled：不播放动画（合成事件同样不会触发动画）。
    send_mouse_to(disabled, QEvent.Type.MouseButtonPress)
    send_mouse_to(disabled, QEvent.Type.MouseButtonRelease)
    pump(0.15)
    assert disabled.press_progress() == 0.0
    assert not disabled.is_press_animating()

    # 快速连击 32 次：动画对象数量回到基线、无运行中动画。
    baseline = len(button.findChildren(QAbstractAnimation))
    started = time.monotonic()
    for _ in range(32):
        send_mouse(QEvent.Type.MouseButtonPress)
        send_mouse(QEvent.Type.MouseButtonRelease)
        QApplication.processEvents()
    pump(0.3)
    elapsed = time.monotonic() - started
    assert len(button.findChildren(QAbstractAnimation)) == baseline, (
        baseline,
        len(button.findChildren(QAbstractAnimation)),
    )
    assert button.press_progress() == 0.0
    assert not button.is_press_animating()
    assert elapsed < 3.0, elapsed

    print(
        f"[buttons] platform={QGuiApplication.platformName()!r}"
        f" normal={normal_bbox} pressed={pressed_bbox}"
        f" pressed_color={_rgb(pressed_target)}"
        f" shrink_w={shrank_w} shrink_h={shrank_h}"
        f" rect={tuple(normal_rect.getRect())}"
        f" rect_delta=0 layout_requests={spy.count}"
        f" anim_baseline={baseline} clicks=32 elapsed={elapsed:.2f}s"
    )
    window.close()
    pump(0.2)
    print("OK press_bounce")


def case_table_hover() -> None:
    """表格整行 hover：delegate 绘制、选中优先、移出恢复、局部重绘计数。"""
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QColor, QMouseEvent
    from PySide6.QtWidgets import QApplication

    window = make_window()
    table = window.pending_table
    assert isinstance(table, ui_qt_main.PendingTable)
    assert table.hasMouseTracking()
    assert table.viewport().hasMouseTracking()
    _fill_pending_rows(window, 3)
    pump(0.3)
    viewport = table.viewport()
    hover_rgb = _rgb(QColor(config.PENDING_ROW_HOVER_BG))
    selected_rgb = _rgb(QColor(config.PENDING_ROW_SELECTED_BG))
    default_rgb = _rgb(QColor(config.CARD))
    input_paths: list[str] = []

    def row_colors(row: int) -> list[tuple[int, int, int]]:
        pixmap = viewport.grab()
        return [
            _rgb(_sample_cell(pixmap, table.visualItemRect(table.item(row, col))))
            for col in range(table.columnCount())
        ]

    def move_to(row: int) -> None:
        rect = table.visualItemRect(table.item(row, 0))
        point = rect.center()
        # 只发合成 MouseMove（控件内逻辑坐标），绝不移动系统光标。
        event = QMouseEvent(
            QEvent.Type.MouseMove,
            QPointF(point),
            QPointF(point),
            Qt.MouseButton.NoButton,
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
        )
        QApplication.sendEvent(viewport, event)
        pump(0.06)
        input_paths.append("synthetic")
        assert table.hovered_row() == row, table.hovered_row()

    move_to(0)
    row0 = row_colors(0)
    assert table.hovered_row() == 0, table.hovered_row()
    assert all(color == hover_rgb for color in row0), row0

    move_to(2)
    assert table.hovered_row() == 2, table.hovered_row()
    row0_after = row_colors(0)
    row2 = row_colors(2)
    assert all(color == default_rgb for color in row0_after), row0_after
    assert all(color == hover_rgb for color in row2), row2
    assert _max_channel_delta(row2) <= 2, row2

    QApplication.sendEvent(viewport, QEvent(QEvent.Type.Leave))
    pump(0.08)
    assert table.hovered_row() == -1, table.hovered_row()
    for row in range(3):
        assert all(color == default_rgb for color in row_colors(row)), row

    # 选中 > hover：选中行被 hover 仍是选中色，移出后选中色不变。
    table.selectRow(2)
    pump(0.1)
    move_to(2)
    assert all(color == selected_rgb for color in row_colors(2)), row_colors(2)
    QApplication.sendEvent(viewport, QEvent(QEvent.Type.Leave))
    pump(0.08)
    assert all(color == selected_rgb for color in row_colors(2)), row_colors(2)
    move_to(0)
    assert all(color == selected_rgb for color in row_colors(2)), row_colors(2)
    assert all(color == hover_rgb for color in row_colors(0)), row_colors(0)

    # 连续 10 次相邻行移动：每次只重绘旧/新两行。
    table.clear_update_records()
    for step in range(10):
        move_to(1 if step % 2 == 0 else 0)
    rects = table.update_rects
    row_heights = {
        table.rowHeight(row) for row in range(table.rowCount())
    }
    update_count = len(rects)
    assert 18 <= update_count <= 22, update_count
    assert all(rect.height() in row_heights for rect in rects), [
        rect.height() for rect in rects
    ]
    assert all(rect.height() < viewport.height() for rect in rects), (
        "出现整表级别的重绘矩形",
        [rect.getRect() for rect in rects],
    )

    print(
        f"[hover] platform={QGuiApplication.platformName()!r}"
        f" hover={hover_rgb} selected={selected_rgb} default={default_rgb}"
        f" row0={row0} row2={row2} moved_updates={update_count}"
        f" max_delta={_max_channel_delta(row2)}"
        f" input={sorted(set(input_paths))}"
    )
    window.close()
    pump(0.2)
    print("OK table_hover")


def case_long_name() -> None:
    """长文件名：先保文件大小，名称 ElideRight，行高不随名称变化。"""
    from qt_controller import PendingRow

    # 205+ 字符的长名需要更短的临时根目录，否则 Windows MAX_PATH 会拦在创建文件时。
    base = Path(
        tempfile.mkdtemp(
            prefix="pfq-", dir=str(Path(__file__).resolve().parent.parent)
        )
    )
    long_name = "a" * 205 + ".png"
    group = make_group(base, [long_name, "s.png", "third.png"])
    workbench = QtReviewWorkbench([group], base)
    show_offscreen(workbench)
    workbench.resize(900, workbench.height())
    pump(0.4)
    workbench.list_widget.viewport().grab()
    info = workbench.list_delegate.last_rows[0]
    line0 = info["lines"][0]
    member1 = info["lines"][1]
    member2 = info["lines"][2]
    assert member1["kind"] == "name_size", member1
    assert member1["name"].endswith("…"), member1["name"]
    assert member1["size"] == format_size(workbench.groups[0].keeper.file_size)
    assert member1["name_x"][1] <= member1["size_x"][0], member1
    assert member1["size_x"][1] <= info["right"] + 0.5, member1
    gap = member1["size_x"][0] - member1["name_x"][1]
    assert gap >= workbench._px(config.WORKBENCH_NAME_GAP), (
        gap,
        workbench._px(config.WORKBENCH_NAME_GAP),
    )
    assert line0["x"][1] <= info["right"] + 0.5, line0
    assert member2["name"] == "s.png", member2["name"]
    assert not member2["name"].endswith("…"), member2["name"]
    for entry in info["lines"]:
        if entry["kind"] == "name_size":
            assert entry["size_x"][1] <= info["right"] + 0.5, entry
            assert entry["name_x"][1] <= entry["size_x"][0], entry
        else:
            assert entry["x"][1] <= info["right"] + 0.5, entry
    heights = {
        workbench.list_widget.item(row).sizeHint().height()
        for row in range(workbench.list_widget.count())
    }
    assert len(heights) == 1, heights
    assert heights.pop() == workbench._px(config.WORKBENCH_LIST_ITEM_HEIGHT)

    # 主表格「概要」列：长名省略、短名不省略、行高一致。
    window = ui_qt_main.PixFilterQtWindow()
    show_offscreen(window)
    window.resize(config.MIN_WINDOW_WIDTH, config.MIN_WINDOW_HEIGHT)
    pump(0.3)
    rows = [
        PendingRow(
            key=1, label="第 1 组", kind="exact", total="3 张",
            summary=long_name, status="⏳待处理",
        ),
        PendingRow(
            key=2, label="第 2 组", kind="exact", total="2 张",
            summary="short.png", status="⏳待处理",
        ),
    ]
    window.refresh_pending(rows)
    pump(0.3)
    table = window.pending_table
    assert table.rowHeight(0) == table.rowHeight(1), (
        table.rowHeight(0), table.rowHeight(1),
    )
    table.viewport().grab()
    delegate = table.itemDelegate()
    long_drawn = delegate.drawn[(0, 3)]
    short_drawn = delegate.drawn[(1, 3)]
    assert long_drawn.endswith("…"), long_drawn
    assert short_drawn == "short.png", short_drawn
    metrics = QFontMetrics(table.font())
    available = table.columnWidth(3) - 2 * config.PENDING_CELL_PAD_X
    assert metrics.horizontalAdvance(long_drawn) <= available, (
        metrics.horizontalAdvance(long_drawn), available,
    )

    print(
        f"[longname] platform={QGuiApplication.platformName()!r}"
        f" name_x=({member1['name_x'][0]:.1f},{member1['name_x'][1]:.1f})"
        f" size_x=({member1['size_x'][0]:.1f},{member1['size_x'][1]:.1f})"
        f" gap={gap:.1f} item=({info['rect'][2]}x{info['rect'][3]})"
        f" table_avail={available} table_row_h={table.rowHeight(0)}"
        f" long_tail={long_drawn[-4:]!r} workbench_w={workbench.width()}"
    )
    window.close()
    workbench.close()
    pump(0.2)
    import shutil

    shutil.rmtree(base, ignore_errors=True)
    print("OK long_name")


def case_g() -> None:
    window = make_window()
    with mock.patch.object(ui_qt_main.webbrowser, "open") as fake_open:
        fake_open.return_value = True
        window.controller.on_github_clicked()
        fake_open.assert_called_once_with("https://github.com/4revo/PixFilter")
    assert config.GITHUB_URL == "https://github.com/4revo/PixFilter"
    for error in (
        ui_qt_main.webbrowser.Error("no browser"),
        OSError("cannot launch"),
        RuntimeError("boom"),
    ):
        with mock.patch.object(ui_qt_main.webbrowser, "open", side_effect=error):
            window.controller.on_github_clicked()  # 不应抛出
        assert "无法打开浏览器" in window.status_label.text()
        assert config.GITHUB_URL in window.status_label.text()
        assert not window._closed
    assert "无法打开浏览器" in window.log_view.toPlainText()
    window.close()
    pump(0.1)
    print("OK g")


def case_h() -> None:
    window = make_window()
    window.resize(config.MIN_WINDOW_WIDTH, config.MIN_WINDOW_HEIGHT)
    assert pump(0.5, lambda: window.width() <= config.MIN_WINDOW_WIDTH + 1)
    hub = window.centralWidget()
    hint = window.hint_label
    github = window.btn_github
    hint_geo = hint.geometry()
    gh_geo = github.geometry()
    assert hint_geo.top() == gh_geo.top() or abs(hint_geo.center().y() - gh_geo.center().y()) <= 2
    assert hint_geo.right() < gh_geo.left()
    assert math_gap(hint_geo, gh_geo) == config.HINT_RIGHT_GAP
    assert gh_geo.right() <= hub.width(), (gh_geo, hub.width())
    assert hint_geo.left() >= 0 and hint_geo.right() <= hub.width()
    assert hint.height() <= hint.fontMetrics().height() + 8
    assert window.minimumSizeHint().width() <= config.MIN_WINDOW_WIDTH
    assert window.minimumSizeHint().height() <= config.MIN_WINDOW_HEIGHT

    buttons = [
        window.btn_choose_folder,
        window.btn_undo,
        window.btn_open_trash,
        window.btn_start,
        window.btn_stop,
    ]
    for button in buttons:
        rect = button.geometry()
        parent = button.parentWidget()
        assert rect.right() <= parent.width(), (button.text(), rect, parent.width())
        assert rect.left() >= 0

    # Bearbeit 长文案截断。
    window.hint_label.set_full_text("提示：" + "很长的说明文字。" * 40)
    pump(0.2)
    shown = window.hint_label.text()
    assert shown.endswith("…"), shown[-20:]
    assert window.hint_label.fontMetrics().horizontalAdvance(shown) <= window.hint_label.width()
    assert github.width() >= github.fontMetrics().horizontalAdvance(github.text())
    window.close()
    pump(0.1)
    print("OK h")


def math_gap(left_rect, right_rect) -> int:
    return right_rect.left() - left_rect.right() - 1


def case_i() -> None:
    scale = float(os.environ.get("PIXFILTER_TEST_SCALE", "1"))
    screen = QGuiApplication.primaryScreen()
    dpi = screen.logicalDotsPerInch()
    ratio = screen.devicePixelRatio()
    # Qt6 采用“逻辑 DPI 固定 96、devicePixelRatio 随缩放变化”的原生方案。
    assert dpi >= 96 * 0.95, (dpi, scale)
    assert ratio >= scale * 0.99, (ratio, scale)
    window = make_window()
    assert abs(window.devicePixelRatioF() - ratio) <= 0.01, (
        window.devicePixelRatioF(),
        ratio,
    )
    button = window.btn_start
    metrics = button.fontMetrics()
    assert button.height() >= metrics.height() + 2 * config.QT_BUTTON_PAD_Y - 2, (
        button.height(),
        metrics.height(),
    )
    assert button.width() >= metrics.horizontalAdvance(button.text()) + 2 * config.QT_BUTTON_PAD_X - 4
    assert button.font().pointSize() == config.FONT_UI[1]
    assert window.threshold_value_label.font().pointSize() == config.FONT_UI[1]
    assert window.status_label.height() > 0
    assert window.minimumWidth() == config.MIN_WINDOW_WIDTH
    assert "configure_dpi_scaling" not in Path(ui_qt_main.__file__).read_text(encoding="utf-8")
    window.close()
    pump(0.1)
    print(f"OK i scale={scale} dpi={dpi:.1f} ratio={ratio:.2f}")


def case_j() -> None:
    window = make_window()
    base = make_temp_base()
    group = make_group(base, ["j1.png", "j2.png"])
    suspended = window._service.add_suspended(group, 1, 1, base)
    window.controller.on_suspended(suspended)
    open_workbench(window)
    pump(0.2)
    window.show_toast("关闭前 Toast")
    assert window._workbench is not None
    assert window._toast is not None
    window.close()
    pump(0.3)
    assert window._closed
    assert not window.isVisible()
    assert window._workbench is None
    assert window._toast is None
    assert window._service.state == "idle"
    assert not [t for t in threading_enumerate() if t.name.startswith("pixfilter-")]
    assert not [
        timer for timer in window.findChildren(QTimer) if timer.isActive()
    ]
    assert not [thread for thread in window.findChildren(QThread) if thread.isRunning()]
    visible = [widget for widget in app().topLevelWidgets() if widget.isVisible()]
    assert visible == [], [widget.windowTitle() for widget in visible]
    print("OK j")


def threading_enumerate():
    import threading

    return threading.enumerate()


def case_scan_e2e() -> None:
    window = make_window()
    base = make_temp_base()
    dup = make_png(base / "dup" / "one.png")
    make_png(base / "dup" / "two.png")
    make_pattern(base / "unique.png")
    window._service.folder = base
    window.controller.on_start_clicked()

    def auto_confirm() -> None:
        # 批处理工作台：确认当前组，工作台会自动推进到空状态。
        workbench = window._workbench
        if workbench is not None:
            workbench._confirm()

    handler = QTimer()
    handler.setInterval(20)
    handler.timeout.connect(auto_confirm)
    handler.start()

    def review_done() -> bool:
        workbench = window._workbench
        return not window.controller.scanning and (
            workbench is None or workbench._complete
        )

    assert pump(8.0, review_done), "批量复核未结束"
    handler.stop()
    assert (base / TRASH_DIR_NAME / "dup" / "two.png").is_file()
    assert dup.is_file()

    # 关闭“全部处理完毕”的工作台后才能撤销。
    workbench = window._workbench
    assert workbench is not None
    workbench.close()
    pump(0.3)
    assert window._workbench is None
    assert window.btn_undo.isEnabled()
    assert "扫描完成" in window.status_label.text()
    assert window._toast is not None
    toast_text = window._toast._label.text()
    assert "扫描完成" in toast_text, toast_text

    window.controller.on_undo_clicked()
    assert pump(3.0, lambda: not window.controller.undo_busy), "撤销未结束"
    assert dup.is_file()
    assert (base / "dup" / "two.png").is_file()
    assert not window.btn_undo.isEnabled()
    assert "撤销" in window.log_view.toPlainText()
    window.close()
    pump(0.2)
    print("OK scan_e2e")


def case_stop() -> None:
    window = make_window()
    base = make_temp_base()
    for index in range(40):
        path = base / f"img_{index:02d}.png"
        Image.new("RGB", (96, 96), (20 + index, 40, 90)).save(path)
    window._service.folder = base
    window.controller.on_start_clicked()
    window.controller.on_stop_clicked()
    assert pump(6.0, lambda: not window.controller.scanning), "停止后未回到空闲"
    assert "已停止（已处理" in window.status_label.text(), window.status_label.text()
    assert window.btn_stop.isEnabled() is False
    assert window.btn_start.isEnabled() is True
    assert window.progress.isHidden()
    assert window.threshold_slider.isEnabled()
    assert window._service.state in ("idle", "scanning")
    window.close()
    pump(0.1)
    print("OK stop")


def case_entry() -> None:
    """经由 main._run_qt 真正进入 Qt 主事件循环：150ms 后自动退出。"""
    import main as app_main

    app_ = ui_qt_main.ensure_qapplication()
    QTimer.singleShot(150, app_.quit)
    code = app_main._run_qt()
    assert code == 0
    assert app_.instance() is app_
    print("OK entry")


def case_scroll_folder() -> None:
    """共享滚动条 QSS + 选择目录按钮的集中状态锁定。"""
    from review_workbench_qt import QtReviewWorkbench

    app_ = app()
    width = review_qt.qt_scrollbar_width()
    assert 6 <= width <= 8, width
    qss = app_.styleSheet()
    assert qss, "应用级滚动条主题未设置"
    radius = max(1, (width - config.QT_SCROLLBAR_MARGIN * 2) // 2)
    for token in (
        "QScrollBar:vertical",
        "QScrollBar:horizontal",
        "QScrollBar::add-line:vertical",
        "QScrollBar::sub-line:vertical",
        "QScrollBar::add-line:horizontal",
        "QScrollBar::sub-line:horizontal",
        "QScrollBar::add-page:vertical",
        "QScrollBar::sub-page:vertical",
        "height: 0px",
        "width: 0px",
        "background: transparent",
        f"background: {config.QT_SCROLLBAR_GROOVE}",
        f"background: {config.QT_SCROLLBAR_HANDLE}",
        config.QT_SCROLLBAR_HANDLE_HOVER,
        config.QT_SCROLLBAR_HANDLE_PRESS,
        f"border-radius: {radius}px",
        f"margin: {config.QT_SCROLLBAR_MARGIN}px",
    ):
        assert token in qss, token

    window = make_window()
    base = make_temp_base()
    window._service.folder = base
    assert window.controller.can_choose_folder()
    assert window.btn_choose_folder.isEnabled()
    assert window.btn_choose_folder.toolTip() == ""

    # 1) 有待处理组：按钮禁用 + tooltip/状态栏提示；主表格滚动条生效。
    groups = [
        make_group(base, [f"s{index}a.png", f"s{index}b.png"])
        for index in range(18)
    ]
    for index, group in enumerate(groups, 1):
        add_pending(window, group, index, len(groups), base)
    pump(0.3)
    assert window.pending_table.rowCount() == 18
    tree_bar = window.pending_table.verticalScrollBar()
    assert tree_bar.isVisible(), "主表格纵向滚动条不可见"
    assert tree_bar.maximum() > 0
    assert tree_bar.width() == width, tree_bar.width()
    tree_bar.setValue(tree_bar.maximum())
    assert tree_bar.value() == tree_bar.maximum()
    tree_bar.setValue(0)
    assert tree_bar.value() == 0

    assert not window.controller.can_choose_folder()
    assert not window.btn_choose_folder.isEnabled()
    tooltip = window.btn_choose_folder.toolTip()
    assert "18 组待处理" in tooltip, tooltip
    assert "待处理" in window.status_label.text()

    # 2) 点击禁用按钮：无对话框、无日志/状态变化、无异常。
    status_before = window.status_label.text()
    log_before = window.log_view.toPlainText()
    with mock.patch.object(ui_qt_main.QFileDialog, "getExistingDirectory") as fake:
        QTest.mouseClick(
            window.btn_choose_folder, Qt.MouseButton.LeftButton
        )
        window.btn_choose_folder.click()
        window.controller.on_choose_folder_clicked()
        assert fake.call_count == 0, fake.call_count
    assert window.status_label.text() == status_before
    assert window.log_view.toPlainText() == log_before

    # 3) 逐组放弃清空后恢复可用。
    abandon_all_pending(window)
    pump(0.3)
    assert window.pending_table.rowCount() == 0
    assert not window._service.pending_groups
    assert window.controller.can_choose_folder()
    assert window.btn_choose_folder.isEnabled()

    # 4) 工作台打开期间禁用；关闭后仍有挂起组则保持禁用。
    wgroups = [
        make_group(
            base, [f"w{index}a.png", f"w{index}b.png", f"w{index}c.png"]
        )
        for index in range(14)
    ]
    window.controller.on_groups_ready(wgroups, base)
    pump(0.4)
    workbench = window._workbench
    assert workbench is not None

    assert not window.controller.can_choose_folder()
    assert not window.btn_choose_folder.isEnabled()
    assert "复核" in window.btn_choose_folder.toolTip()

    list_bar = workbench.list_widget.verticalScrollBar()
    assert list_bar.isVisible(), "工作台左栏滚动条不可见"
    assert list_bar.maximum() > 0
    assert list_bar.width() == width, list_bar.width()
    h_bar = workbench.scroll.horizontalScrollBar()
    assert h_bar.isVisible(), "工作台右侧横向滚动条不可见"
    assert h_bar.maximum() > 0
    assert h_bar.height() == width, h_bar.height()
    h_bar.setValue(h_bar.maximum())
    assert h_bar.value() == h_bar.maximum()
    h_bar.setValue(0)
    list_bar.setValue(list_bar.maximum())
    assert list_bar.value() == list_bar.maximum()
    list_bar.setValue(0)

    workbench.close()
    pump(0.6)
    assert window._workbench is None
    # 关闭工作台后手动登记挂起组，模拟「关闭但仍有待处理」。
    for index, group in enumerate(wgroups, 1):
        add_pending(window, group, index, len(wgroups), base)
    pump(0.3)
    assert not window.controller.can_choose_folder()
    assert not window.btn_choose_folder.isEnabled()
    assert len(window._service.pending_groups) == 14
    assert "14 组待处理" in window.btn_choose_folder.toolTip()

    # 5) 再次清空后恢复；扫描中禁用；撤销中禁用。
    abandon_all_pending(window)
    pump(0.3)
    assert not window._service.pending_groups
    assert window.controller.can_choose_folder()
    assert window.btn_choose_folder.isEnabled()

    with mock.patch.object(window._service, "start_scan", return_value=True):
        window.controller.on_start_clicked()
    assert window.controller.scanning is True
    assert not window.controller.can_choose_folder()
    assert not window.btn_choose_folder.isEnabled()
    assert "扫描" in window.btn_choose_folder.toolTip()
    window.controller._scanning = False
    window.controller._refresh()

    window.controller._undo_busy = True
    window.controller._refresh()
    assert not window.controller.can_choose_folder()
    assert not window.btn_choose_folder.isEnabled()
    assert "撤销" in window.btn_choose_folder.toolTip()
    window.controller._undo_busy = False
    window.controller._refresh()
    assert window.controller.can_choose_folder()
    assert window.btn_choose_folder.isEnabled()

    window.close()
    pump(0.2)
    print("OK scroll_folder")


def case_ports() -> None:
    """端口完整性：真实 View 实现了 MainViewPort 的每个方法，且控制器不摸控件。"""
    import qt_controller

    window = make_window()
    names = [
        name
        for name in dir(qt_controller.MainViewPort)
        if not name.startswith("_")
    ]
    missing = [
        name for name in names if not callable(getattr(window, name, None))
    ]
    assert not missing, f"View 缺少端口方法：{missing}"
    assert isinstance(window.controller, qt_controller.ScanController)
    for widget_attr in (
        "btn_choose_folder",
        "btn_start",
        "btn_stop",
        "btn_undo",
        "pending_table",
        "status_label",
        "progress",
        "log_view",
    ):
        assert not hasattr(
            window.controller, widget_attr
        ), f"控制器不应持有控件：{widget_attr}"
    window.close()
    pump(0.2)
    print("OK ports")


def case_controller_wiring() -> None:
    """真实 View + Controller + Service：扫描 → 工作台 → 确认 → 撤销。"""
    window = make_window()
    base = make_temp_base()
    make_png(base / "a.png")
    make_png(base / "b.png")

    snapshots = []
    original = window.refresh_pending

    def wrapped(rows) -> None:
        keys = [row.key for row in rows]
        assert len(keys) == len(set(keys)), f"重复行：{keys}"
        snapshots.append(tuple(keys))
        original(rows)

    window.refresh_pending = wrapped
    window._service.folder = base
    window.controller.on_start_clicked()

    assert pump(10.0, lambda: window._workbench is not None), "工作台未打开"
    assert window.controller.can_choose_folder() is False
    assert window.btn_choose_folder.isEnabled() is False
    assert "批量复核" in window.btn_choose_folder.toolTip()

    workbench = window._workbench
    workbench._confirm()
    assert pump(8.0, lambda: workbench._complete), "复核未完成"
    workbench.close()
    pump(0.5)
    assert window._workbench is None
    assert window.btn_choose_folder.isEnabled() == window.controller.can_choose_folder()
    assert window.btn_choose_folder.isEnabled() is True
    assert window.btn_undo.isEnabled() == window.controller._undo_enabled()
    assert window.btn_undo.isEnabled() is True
    assert window.controller.undo_busy is False
    assert window.controller.pending_busy is False
    assert len(snapshots) <= 20, f"刷新次数异常：{len(snapshots)}"

    window.controller.on_undo_clicked()
    assert pump(4.0, lambda: not window.controller.undo_busy), "撤销未结束"
    assert (base / "b.png").exists(), "撤销后文件未恢复"
    window.close()
    pump(0.3)
    print("OK controller_wiring")


def _assert_no_groove_column(widget, window, geom_widget) -> None:
    """采样控件右边缘 20px：空态下不允许出现旧槽色 #F5F5F5 的竖条。"""
    img = window.grab().toImage()
    origin = geom_widget.mapTo(window, QPoint(0, 0))
    left = origin.x() + geom_widget.width() - 20
    right = origin.x() + geom_widget.width() + 1
    top = origin.y() + 2
    bottom = origin.y() + geom_widget.height() - 1
    offenders = []
    for x in range(max(0, left), min(img.width(), right)):
        for y in range(max(0, top), min(img.height(), bottom)):
            if img.pixelColor(x, y).name() == "#F5F5F5":
                offenders.append((x, y))
    assert not offenders, f"{widget} 右边缘残留槽色像素：{offenders[:6]}"


def case_scroll_empty() -> None:
    """空态无滚动条/无灰线；溢出才出现；回到空态无残影。"""
    from review_workbench_qt import QtReviewWorkbench  # noqa: F401

    app_ = app()
    width = review_qt.qt_scrollbar_width()
    qss = app_.styleSheet()
    assert config.QT_SCROLLBAR_GROOVE == "transparent", config.QT_SCROLLBAR_GROOVE
    assert f"background: {config.QT_SCROLLBAR_GROOVE}" in qss
    assert f"background: {config.QT_SCROLLBAR_HANDLE}" in qss, "handle 不应被透明化"

    window = make_window()
    base = make_temp_base()
    tree_bar = window.pending_table.verticalScrollBar()
    card = window.pending_stack.parentWidget()

    # a) 空态：没有可见滚动条，右边缘没有灰色竖条。
    assert window.pending_stack.currentWidget() is window.pending_empty
    assert not tree_bar.isVisible(), "空态滚动条仍可见"
    assert tree_bar.maximum() == 0
    assert (
        window.pending_table.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    _assert_no_groove_column("pending-empty", window, card)
    log_bar = window.log_view.verticalScrollBar()
    assert (
        window.log_view.verticalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    assert (
        window.log_view.horizontalScrollBarPolicy()
        == Qt.ScrollBarPolicy.ScrollBarAsNeeded
    )
    assert not log_bar.isVisible(), "日志区未溢出却显示滚动条"

    # b) 有数据且溢出：滚动条出现、宽度按 DPI、可滚动。
    groups = [
        make_group(base, [f"e{index}a.png", f"e{index}b.png"])
        for index in range(18)
    ]
    for index, group in enumerate(groups, 1):
        add_pending(window, group, index, len(groups), base)
    pump(0.3)
    assert window.pending_table.rowCount() == 18
    assert tree_bar.isVisible(), "溢出时滚动条未出现"
    assert tree_bar.maximum() > 0
    assert tree_bar.width() == width, tree_bar.width()
    tree_bar.setValue(tree_bar.maximum())
    assert tree_bar.value() == tree_bar.maximum()

    # c) 回到空态：滚动条重新隐藏且无残影。
    abandon_all_pending(window)
    pump(0.3)
    assert window.pending_table.rowCount() == 0
    assert window.pending_stack.currentWidget() is window.pending_empty
    assert not tree_bar.isVisible(), "清空后滚动条残留"
    assert tree_bar.maximum() == 0
    _assert_no_groove_column("pending-after-clear", window, card)

    # 工作台左栏：未溢出时隐藏，溢出时才出现。
    small = [
        make_group(base, [f"l{index}a.png", f"l{index}b.png"])
        for index in range(3)
    ]
    window.controller.on_groups_ready(small, base)
    pump(0.4)
    workbench = window._workbench
    assert workbench is not None
    list_bar = workbench.list_widget.verticalScrollBar()
    assert not list_bar.isVisible(), "左栏未溢出却显示滚动条"
    assert list_bar.maximum() == 0
    _assert_no_groove_column("workbench-left", workbench, workbench.list_widget)

    workbench.close()
    pump(0.4)
    many = [
        make_group(base, [f"m{index}a.png", f"m{index}b.png"])
        for index in range(14)
    ]
    window.controller.on_groups_ready(many, base)
    pump(0.4)
    workbench = window._workbench
    assert workbench is not None
    list_bar = workbench.list_widget.verticalScrollBar()
    assert list_bar.isVisible(), "左栏溢出时滚动条未出现"
    assert list_bar.maximum() > 0
    assert list_bar.width() == width, list_bar.width()
    list_bar.setValue(list_bar.maximum())
    assert list_bar.value() == list_bar.maximum()

    workbench.close()
    pump(0.3)
    window.close()
    pump(0.3)
    print("OK scroll_empty")


def case_pending_workbench() -> None:
    """待处理面板：选中高亮、双击/继续 → 同一个工作台、旧弹窗零调用。"""
    dialog_calls: list[int] = []
    window = make_window()
    window.show_review_dialog = lambda *args, **kwargs: dialog_calls.append(1)
    base = make_temp_base()
    groups = [
        make_group(base, [f"pw{i}a.png", f"pw{i}b.png"]) for i in range(4)
    ]
    for index, group in enumerate(groups, 1):
        add_pending(window, group, index, len(groups), base)
    pump(0.3)
    table = window.pending_table
    assert table.rowCount() == 4
    assert not hasattr(
        ui_qt_main.PixFilterQtWindow, "show_review_dialog"
    ), "主界面仍保留单组弹窗入口"
    assert table.selectionBehavior() == QAbstractItemView.SelectionBehavior.SelectRows
    assert table.selectionMode() == QAbstractItemView.SelectionMode.SingleSelection
    assert table.editTriggers() == QAbstractItemView.EditTrigger.NoEditTriggers
    qss = table.styleSheet()
    assert "QTableWidget::item:selected" in qss
    assert config.PENDING_ROW_SELECTED_BG in qss.lower(), "选中底色未写入 QSS"

    # a) 单击第 2 行：整行单选 + 选中底色真实绘制。
    rect = table.visualItemRect(table.item(2, 0))
    QTest.mouseClick(
        table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
    )
    pump(0.1)
    assert table.currentRow() == 2, table.currentRow()
    assert len(table.selectedItems()) == table.columnCount()
    image = table.viewport().grab().toImage()
    color = image.pixelColor(
        rect.right() - 8, rect.center().y()
    ).name()
    assert color == config.PENDING_ROW_SELECTED_BG, color

    # b) 双击第 1 行 → 工作台存在且聚焦该组，卡片数 = 成员数。
    rect = table.visualItemRect(table.item(1, 0))
    QTest.mouseClick(
        table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
    )
    QTest.mouseDClick(
        table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
    )
    pump(0.4)
    workbench = window._workbench
    assert workbench is not None
    assert workbench.current_index == 1, workbench.current_index
    # 单选已删除：成员数改为卡片数校验，默认 keeper 仍由 pick_default_keeper 决定。
    assert len(workbench._cards) == 2, len(workbench._cards)
    assert workbench._selected_path() == str(workbench.default.path)

    # c) 关闭后「继续处理」：无选中 → 含全部组；重复点击不产生第二个窗口。
    workbench.close()
    pump(0.4)
    assert window._workbench is None
    table.clearSelection()
    QTest.mouseClick(
        window.btn_pending_continue, Qt.MouseButton.LeftButton
    )
    pump(0.4)
    workbench = window._workbench
    assert workbench is not None
    assert len(workbench.groups) == 4
    assert workbench.current_index == 0
    QTest.mouseClick(
        window.btn_pending_continue, Qt.MouseButton.LeftButton
    )
    pump(0.3)
    assert window._workbench is workbench, "重复点击创建了第二个工作台"
    assert not window.btn_pending_continue.isEnabled(), "工作台打开时按钮应禁用"
    instances = [
        widget
        for widget in app().topLevelWidgets()
        if isinstance(widget, QtReviewWorkbench)
    ]
    assert len(instances) == 1, len(instances)

    # 有选中 → 继续处理（工作台已开，直接驱动控制器复用实例）聚焦该组。
    select_pending_row(window, 2)
    window.controller.on_resume_pending()
    pump(0.3)
    assert window._workbench is workbench
    assert workbench.current_index == 2, workbench.current_index

    # d) 双击同一行仍复用实例，且旧单组弹窗路径全程零调用。
    rect = table.visualItemRect(table.item(3, 0))
    QTest.mouseClick(
        table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
    )
    QTest.mouseDClick(
        table.viewport(), Qt.MouseButton.LeftButton, pos=rect.center()
    )
    pump(0.3)
    assert window._workbench is workbench
    assert workbench.current_index == 3
    assert dialog_calls == [], dialog_calls

    # e) 含缺失文件的挂起组：占位显示，confirm 跳过缺失项且不崩、行移除。
    workbench.close()
    pump(0.4)
    group = make_group(base, ["m1.png", "m2.png"])
    add_pending(window, group, 5, 5, base)
    pump(0.2)
    missing = group.duplicates[0].path
    missing.unlink()
    row = table.rowCount() - 1
    workbench = focus_row_workbench(window, row)
    assert len(workbench._placeholders) >= 1, "缺失文件未显示占位"
    assert (base / "m1.png").is_file()
    workbench._decide_current("confirm")
    pump(0.3)
    assert table.rowCount() == 4, table.rowCount()
    assert not (base / TRASH_DIR_NAME / "m2.png").exists()
    assert "已跳过不存在的文件" in window.log_view.toPlainText()
    assert window.controller.workbench_open

    window.close()
    pump(0.3)
    print("OK pending_workbench")


def _pixel_runs(pixmap, rect, backgrounds):
    """区域内非背景像素的竖直连续段：[(color, x, y_start, length)]。"""
    image = pixmap.toImage()
    ratio = pixmap.devicePixelRatio() or 1.0
    columns = {}
    left, top, width, height = rect
    for y in range(top, top + height):
        for x in range(left, left + width):
            px = int(round(x * ratio))
            py = int(round(y * ratio))
            if not (0 <= px < image.width() and 0 <= py < image.height()):
                continue
            color = image.pixelColor(px, py).name().lower()
            if color in backgrounds:
                continue
            columns.setdefault((x, color), []).append(y)
    runs = []
    for (x, color), ys in columns.items():
        ys.sort()
        start = prev = ys[0]
        for y in ys[1:]:
            if y == prev + 1:
                prev = y
                continue
            runs.append((color, x, start, prev - start + 1))
            start = prev = y
        runs.append((color, x, start, prev - start + 1))
    return runs


def _fill_pending_rows(window, rows: int) -> None:
    window.pending_stack.setCurrentWidget(window.pending_table)
    window.pending_table.setRowCount(rows)
    for row in range(rows):
        for column in range(window.pending_table.columnCount()):
            item = window.pending_table.item(row, column) or ui_qt_main.QTableWidgetItem()
            item.setText(f"r{row}c{column}")
            window.pending_table.setItem(row, column, item)


def case_doctor_offscreen() -> None:
    """样式/接线兜底：表格/表头/容器/角按钮去边框；进度条几何与像素。"""
    from PySide6.QtCore import QMargins
    from PySide6.QtWidgets import QProgressBar, QWidget

    window = make_window()
    table = window.pending_table
    header = table.horizontalHeader()
    assert table.frameShape() == QFrame.Shape.NoFrame
    assert table.lineWidth() == 0
    assert table.midLineWidth() == 0
    assert table.showGrid() is False
    assert header.frameShape() == QFrame.Shape.NoFrame

    sheet = table.styleSheet()
    for token in (
        "QTableWidget {",
        "QHeaderView {",
        "QHeaderView::section",
        "QTableCornerButton::section",
        "border: none",
    ):
        assert token in sheet, token
    assert "border: 1px" not in sheet, sheet

    stack_sheet = window.pending_stack.styleSheet()
    assert "QStackedWidget#pendingStack" in stack_sheet, stack_sheet
    assert "border: none" in stack_sheet, stack_sheet
    assert "transparent" in stack_sheet, stack_sheet
    assert window.pending_empty.contentsMargins() == QMargins(0, 0, 0, 0)
    assert "border: none" in window.pending_empty.styleSheet()

    card = window.pending_stack.parentWidget()
    assert "QFrame#cardFrame" in card.styleSheet(), card.styleSheet()
    assert "QFrame {" not in card.styleSheet(), card.styleSheet()
    assert table.findChild(QWidget, "qt_scrollarea_cornerwidget") is None

    assert isinstance(window.progress, QProgressBar)
    progress_sheet = window.progress.styleSheet()
    for token in (
        f"border-radius: {config.PROGRESS_RADIUS}px",
        f"padding: {config.PROGRESS_PADDING}px",
        f"background-color: {config.PROGRESS_TROUGH}",
        f"border-radius: {config.PROGRESS_CHUNK_RADIUS}px",
        f"background-color: {config.ACCENT}",
    ):
        assert token in progress_sheet, token
    assert window.progress.height() == config.PROGRESS_THICKNESS

    window.progress.setVisible(True)
    pump(0.2)
    origin = window.progress.mapTo(window, QPoint(0, 0))
    distance = window.width() - (origin.x() + window.progress.width())
    print(f"progress distance={distance} width={window.progress.width()} "
          f"height={window.progress.height()}")
    assert distance >= 10, distance
    assert distance >= config.PROGRESS_SIDE_MARGIN, distance

    accent = config.ACCENT.lower()
    window.progress.setValue(0)
    pump(0.1)
    image = window.progress.grab().toImage()
    ratio = window.progress.grab().devicePixelRatio() or 1.0
    zero_accent = [
        (x, y)
        for y in range(window.progress.height())
        for x in range(window.progress.width())
        if image.pixelColor(
            int(round(x * ratio)), int(round(y * ratio))
        ).name().lower() == accent
    ]
    assert not zero_accent, f"value=0 出现 chunk 像素：{zero_accent[:5]}"

    window.progress.setValue(100)
    pump(0.1)
    image = window.progress.grab().toImage()
    ratio = window.progress.grab().devicePixelRatio() or 1.0
    width, height = window.progress.width(), window.progress.height()
    center = image.pixelColor(
        int(round((width // 2) * ratio)), int(round((height // 2) * ratio))
    ).name().lower()
    corners = [
        image.pixelColor(int(round(x * ratio)), int(round(y * ratio))).name().lower()
        for x, y in ((1, 1), (width - 2, 1), (1, height - 2), (width - 2, height - 2))
    ]
    assert center == accent, (center, accent)
    assert all(color != accent for color in corners), corners

    window.progress.setValue(0)
    window.progress.hide()
    window.close()
    pump(0.2)
    print("OK doctor_offscreen")


def case_doctor_pixels() -> None:
    """离屏像素扫描（offscreen + grab）：确认无游离灰线，证据强度与原真机用例一致。"""
    from PySide6.QtGui import QGuiApplication

    app_ = app()
    window = ui_qt_main.PixFilterQtWindow()
    show_offscreen(window)
    pump(0.5)
    platform = QGuiApplication.platformName()

    backgrounds = {
        config.CARD.lower(),
        config.PENDING_EMPTY_BG.lower(),
        config.BG.lower(),
        "#ffffff",
    }

    # a) 空态：待处理计数标签右缘（旧 bug 的 1px×18px 灰线位置）。
    window.pending_count_label.setText("")
    pump(0.1)
    label = window.pending_count_label
    origin = label.mapTo(window, QPoint(0, 0))
    rect = (origin.x() - 1, origin.y() - 1, label.width() + 2, label.height() + 2)
    runs = _pixel_runs(window.grab(), rect, backgrounds)
    offenders = [run for run in runs if run[3] >= 2]
    assert not offenders, f"计数标签右缘仍有竖线：{offenders}"
    assert all(run[0] != config.BORDER.lower() for run in runs), runs

    # 空态：面板内容右上 48×48 区域，不允许任何 ≥2px 的竖直灰线。
    panel = window.pending_empty
    origin = panel.mapTo(window, QPoint(0, 0))
    region = (origin.x() + panel.width() - 48, origin.y(), 48, 48)
    runs = _pixel_runs(window.grab(), region, backgrounds)
    long_runs = [run for run in runs if run[3] >= 2]
    print(
        f"empty region runs={len(runs)} max_run="
        f"{max((run[3] for run in runs), default=0)} colors="
        f"{sorted({run[0] for run in runs})}"
    )
    assert not long_runs, f"空态右上仍有竖线：{long_runs}"

    # 卡片头部右上角（计数标签所在行）：只允许卡片自身的 1px 描边。
    card = window.pending_stack.parentWidget()
    card_origin = card.mapTo(window, QPoint(0, 0))
    card_right = card_origin.x() + card.width() - 1
    header_region = (card_right - 47, card_origin.y(), 48, 48)
    header_runs = _pixel_runs(window.grab(), header_region, backgrounds)
    header_border = [
        run
        for run in header_runs
        if run[3] >= 2
        and run[0] == config.BORDER.lower()
        and run[1] == card_right
    ]
    header_bad = [
        run
        for run in header_runs
        if run[3] >= 2 and run not in header_border
    ]
    print(
        f"header region card_right={card_right} border_runs={header_border}"
        f" other_runs={header_bad}"
    )
    assert not header_bad, f"卡片头部右上仍有额外竖线：{header_bad}"

    # b) 18 组溢出：只允许 scrollbar handle 出现在 8px 轨道内。
    _fill_pending_rows(window, 18)
    pump(0.4)
    bar = window.pending_table.verticalScrollBar()
    assert bar.isVisible(), "18 组未出现滚动条"
    assert bar.maximum() > 0
    viewport = window.pending_table.viewport()
    vp_origin = viewport.mapTo(window, QPoint(0, 0))
    bar_origin = bar.mapTo(window, QPoint(0, 0))
    strip = (
        vp_origin.x() + viewport.width(),
        bar_origin.y(),
        bar.width() + 2,
        bar.height(),
    )
    runs = _pixel_runs(window.grab(), strip, backgrounds)
    allowed = {
        config.QT_SCROLLBAR_HANDLE.lower(),
        config.QT_SCROLLBAR_HANDLE_HOVER.lower(),
        config.QT_SCROLLBAR_HANDLE_PRESS.lower(),
    }
    bad = [
        run
        for run in runs
        if run[3] >= 2
        and (
            run[0] not in allowed
            or not (bar_origin.x() <= run[1] < bar_origin.x() + bar.width())
        )
    ]
    assert not bad, f"滚动条轨道外出现灰线：{bad}"
    handle_runs = [run for run in runs if run[3] >= 2]
    assert handle_runs, "溢出态未见 handle（渲染异常）"
    print(
        f"platform={platform!r} bar=({bar_origin.x()},{bar_origin.y()},"
        f"{bar.width()},{bar.height()}) handle={handle_runs[:3]}"
    )

    window.progress.setVisible(True)
    window.progress.setValue(0)
    pump(0.2)
    origin = window.progress.mapTo(window, QPoint(0, 0))
    distance = window.width() - (origin.x() + window.progress.width())
    assert distance >= 10, f"进度条右缘距窗口仅 {distance}px"
    accent = config.ACCENT.lower()
    pixmap = window.progress.grab()
    ratio = pixmap.devicePixelRatio() or 1.0
    image = pixmap.toImage()

    def _color_at(x: int, y: int) -> str:
        return image.pixelColor(
            int(round(x * ratio)), int(round(y * ratio))
        ).name().lower()

    width, height = window.progress.width(), window.progress.height()
    zero_accent = sum(
        1
        for yy in range(height)
        for xx in range(width)
        if _color_at(xx, yy) == accent
    )
    assert zero_accent == 0, f"value=0 仍有 chunk 像素 {zero_accent}"
    window.progress.setValue(100)
    pump(0.2)
    pixmap = window.progress.grab()
    ratio = pixmap.devicePixelRatio() or 1.0
    image = pixmap.toImage()
    corners = [(1, 1), (width - 2, 1), (1, height - 2), (width - 2, height - 2)]
    corner_colors = [_color_at(x, y) for x, y in corners]
    assert all(color != accent for color in corner_colors), corner_colors
    print(
        f"progress real bar=({origin.x()},{origin.y()},{width},{height})"
        f" right_gap={distance} zero_accent={zero_accent}"
        f" corner_colors={corner_colors}"
    )

    window.close()
    pump(0.3)
    print("OK doctor_pixels")


def _file_snapshot(base: Path) -> list[tuple[str, int, bytes]]:
    """目录快照：相对路径 + 大小 + 内容，用于证明“零移动/零删除”。"""
    return sorted(
        (
            path.relative_to(base).as_posix(),
            path.stat().st_size,
            path.read_bytes(),
        )
        for path in base.rglob("*")
        if path.is_file()
    )


def _log_line_count(window) -> int:
    return len(window.log_view.toPlainText().splitlines())


def case_abandon_selected() -> None:
    """「放弃处理选中组」只移除选中一组；无选中时禁用且零副作用。"""
    window = make_window()
    base = make_temp_base()
    groups = []
    for index in range(3):
        group = make_group(base, [f"g{index}a.png", f"g{index}b.png"])
        groups.append(group)
        add_pending(window, group, index + 1, 3, base)
    pump(0.2)
    table = window.pending_table
    assert table.rowCount() == 3, table.rowCount()
    assert window.btn_pending_abandon_all.isEnabled()
    assert window.btn_pending_abandon.isEnabled(), "默认首行选中时单选放弃应可用"

    before_files = _file_snapshot(base)
    keys_before = [
        table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        for row in range(3)
    ]

    select_pending_row(window, 1)
    assert table.currentRow() == 1
    abandoned_key = keys_before[1]
    log_before = _log_line_count(window)
    QTest.mouseClick(
        window.btn_pending_abandon, Qt.MouseButton.LeftButton
    )
    pump(0.2)

    assert table.rowCount() == 2, table.rowCount()
    keys_after = [
        table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        for row in range(table.rowCount())
    ]
    assert abandoned_key not in keys_after, keys_after
    assert keys_after == [keys_before[0], keys_before[2]], keys_after
    assert _file_snapshot(base) == before_files, "放弃不应移动或删除任何文件"
    assert _log_line_count(window) == log_before + 1
    assert "[放弃处理]" in window.log_view.toPlainText()

    # 无选中：按钮禁用 + tooltip 说明原因 + 点击零副作用。
    table.clearSelection()
    pump(0.1)
    window.controller.on_pending_selection_changed([])
    window.controller._sync_buttons()
    assert window.btn_pending_abandon.isEnabled() is False
    assert "请先" in window.btn_pending_abandon.toolTip()
    rows_before = table.rowCount()
    files_before = _file_snapshot(base)
    logs_before = _log_line_count(window)
    QTest.mouseClick(
        window.btn_pending_abandon, Qt.MouseButton.LeftButton
    )
    pump(0.1)
    assert table.rowCount() == rows_before
    assert _file_snapshot(base) == files_before
    assert _log_line_count(window) == logs_before

    # 直接调用控制器：状态栏提示且不移动。
    window.controller.on_abandon_pending()
    assert window.status_label.text() == "请先在待处理列表选择一组"
    assert table.rowCount() == rows_before
    assert _file_snapshot(base) == files_before

    window.close()
    pump(0.2)
    print(
        "abandon_selected rows=2 files="
        f"{len(before_files)} log_growth=1"
    )
    print("OK abandon_selected")


def case_abandon_all() -> None:
    """「全部放弃」四分支：取消 / 确定 / 无组 / 扫描中 / 工作台打开。"""
    window = make_window()
    base = make_temp_base()
    for index in range(5):
        group = make_group(base, [f"a{index}.png", f"b{index}.png"])
        add_pending(window, group, index + 1, 5, base)
    pump(0.2)
    table = window.pending_table
    assert table.rowCount() == 5, table.rowCount()

    # 二次确认框文案（只构造、不 exec，避免模态框阻塞）。
    box = ui_qt_main._build_abandon_all_box(window)
    assert box.text() == config.PENDING_ABANDON_ALL_CONFIRM_TEXT, box.text()
    assert box.windowTitle() == config.PENDING_ABANDON_ALL_CONFIRM_TITLE
    labels = sorted(button.text() for button in box.buttons())
    assert labels == sorted(
        [
            config.PENDING_ABANDON_ALL_CONFIRM_YES,
            config.PENDING_ABANDON_ALL_CONFIRM_NO,
        ]
    ), labels
    box.close()

    calls = {"count": 0, "answer": False}

    def fake_confirm() -> bool:
        calls["count"] += 1
        return calls["answer"]

    window.confirm_abandon_all = fake_confirm

    # a) 取消：表格 / 状态栏 / 日志 / 按钮 / 文件均不变。
    status_before = window.status_label.text()
    files_before = _file_snapshot(base)
    logs_before = _log_line_count(window)
    states_before = (
        window.btn_pending_abandon_all.isEnabled(),
        window.btn_pending_abandon.isEnabled(),
        window.btn_pending_continue.isEnabled(),
    )
    QTest.mouseClick(
        window.btn_pending_abandon_all, Qt.MouseButton.LeftButton
    )
    pump(0.2)
    assert calls["count"] == 1, calls
    assert table.rowCount() == 5, table.rowCount()
    assert window.status_label.text() == status_before
    assert _log_line_count(window) == logs_before
    assert _file_snapshot(base) == files_before
    assert (
        window.btn_pending_abandon_all.isEnabled(),
        window.btn_pending_abandon.isEnabled(),
        window.btn_pending_continue.isEnabled(),
    ) == states_before

    # b) 确定：清空全部、空态、选择目录恢复、状态精确、日志一条、零移动。
    calls["answer"] = True
    files_before = _file_snapshot(base)
    logs_before = _log_line_count(window)
    QTest.mouseClick(
        window.btn_pending_abandon_all, Qt.MouseButton.LeftButton
    )
    pump(0.2)
    assert calls["count"] == 2, calls
    assert table.rowCount() == 0, table.rowCount()
    assert window.pending_stack.currentWidget() is window.pending_empty
    assert window.controller.can_choose_folder()
    assert window.btn_choose_folder.isEnabled()
    assert window.status_label.text() == "已全部放弃", window.status_label.text()
    assert _log_line_count(window) == logs_before + 1
    text = window.log_view.toPlainText()
    assert "[全部放弃]" in text
    assert "a0.png" in text and "b4.png" in text, text[-200:]
    assert _file_snapshot(base) == files_before, "全部放弃不应移动或删除任何文件"
    assert window.btn_pending_abandon_all.isEnabled() is False
    assert window.btn_pending_abandon.isEnabled() is False
    assert window.btn_pending_continue.isEnabled() is False

    # c) 无待处理组：按钮禁用，程序化 click 不触发确认。
    before = calls["count"]
    QTest.mouseClick(
        window.btn_pending_abandon_all, Qt.MouseButton.LeftButton
    )
    pump(0.1)
    assert calls["count"] == before, calls

    # d) 扫描中：有挂起组但 busy → 禁用，click 不触发确认。
    add_pending(
        window, make_group(base, ["s1.png", "s2.png"]), 6, 6, base
    )
    pump(0.1)
    assert window.btn_pending_abandon_all.isEnabled()
    window.controller._scanning = True
    window.controller._sync_buttons()
    assert window.btn_pending_abandon_all.isEnabled() is False
    QTest.mouseClick(
        window.btn_pending_abandon_all, Qt.MouseButton.LeftButton
    )
    pump(0.1)
    assert calls["count"] == before, calls
    window.controller._scanning = False
    window.controller._sync_buttons()

    # e) 工作台打开：同样禁用，click 不触发确认。
    assert window.btn_pending_abandon_all.isEnabled()
    window.controller.on_resume_pending()
    pump(0.4)
    assert window.controller.workbench_open
    assert window.btn_pending_abandon_all.isEnabled() is False
    QTest.mouseClick(
        window.btn_pending_abandon_all, Qt.MouseButton.LeftButton
    )
    pump(0.1)
    assert calls["count"] == before, calls
    assert window._workbench is not None
    window._workbench.close()
    pump(0.3)

    window.close()
    pump(0.2)
    print(
        f"abandon_all cancel_rows=5 cancel_log_delta=0"
        f" confirm_rows=0 files={len(files_before)}"
        f" confirm_calls={calls['count']}"
    )
    print("OK abandon_all")


def case_pending_footer_layout() -> None:
    """底部三按钮：顺序、间距、垂直中线、右缘对齐与最小宽度适配。"""
    import inspect

    window = make_window()
    all_button = window.btn_pending_abandon_all
    abandon_button = window.btn_pending_abandon
    continue_button = window.btn_pending_continue

    # 红色按钮 QSS 引用 config 常量（源码里不出现字面量）。
    sheet = all_button.styleSheet()
    for token in (
        config.PENDING_ABANDON_ALL_BG,
        config.PENDING_ABANDON_ALL_HOVER,
        config.PENDING_ABANDON_ALL_PRESS,
        config.PENDING_ABANDON_ALL_DISABLED_BG,
    ):
        assert token in sheet, token
    source = inspect.getsource(ui_qt_main.QtButton._style)
    assert "PENDING_ABANDON_ALL_BG" in source
    assert config.PENDING_ABANDON_ALL_BG.lower() not in source.lower(), (
        "红色背景不应硬编码到视图"
    )

    assert window.pending_footer.spacing() == config.PENDING_FOOTER_GAP
    buttons = [all_button, abandon_button, continue_button]
    widths_by_scale: dict[int, list[int]] = {}

    for width, height in (
        (config.WINDOW_WIDTH, config.WINDOW_HEIGHT),
        (config.MIN_WINDOW_WIDTH, config.MIN_WINDOW_HEIGHT),
    ):
        window.resize(width, height)
        assert pump(0.6, lambda: window.width() <= width + 1)
        pump(0.2)
        coords = []
        for button in buttons:
            origin = button.mapTo(window, QPoint(0, 0))
            coords.append(
                (origin.x(), origin.y(), button.width(), button.height())
            )
        widths_by_scale[width] = [item[2] for item in coords]

        centers = [item[1] + item[3] / 2 for item in coords]
        y_delta = max(centers) - min(centers)
        assert y_delta <= 2, (width, y_delta, centers)

        # 顺序：全部放弃 < 放弃处理选中组 < 继续处理（x 由小到大）。
        assert coords[0][0] < coords[1][0] < coords[2][0], coords
        gaps = [
            coords[index + 1][0] - (coords[index][0] + coords[index][2])
            for index in range(2)
        ]
        assert all(8 <= gap <= 12 for gap in gaps), (width, gaps)

        card = window.pending_stack.parentWidget()
        card_origin = card.mapTo(window, QPoint(0, 0))
        card_content_right = (
            card_origin.x() + card.contentsRect().right() + 1
        )
        right_margin = card_content_right - (coords[2][0] + coords[2][2])
        assert right_margin == config.PENDING_INNER_PADX, (
            width,
            right_margin,
        )

        right_edge = coords[2][0] + coords[2][2]
        assert right_edge <= card.width() + card_origin.x()
        assert coords[0][0] >= card_origin.x()
        hint = window.pending_footer_hint
        hint_origin = hint.mapTo(window, QPoint(0, 0))
        assert hint_origin.x() + hint.width() <= coords[0][0], (
            width,
            hint_origin,
            coords[0],
        )

        print(
            f"footer w={width} y_delta={y_delta:.1f} gaps={gaps}"
            f" right_margin={right_margin}"
            f" all={coords[0]} abandon={coords[1]} continue={coords[2]}"
        )

    # 宽度不随窗口缩放变化（文本固定，布局只移动位置）。
    for column in range(3):
        values = [widths_by_scale[w][column] for w in widths_by_scale]
        assert max(values) - min(values) <= 2, values

    window.close()
    pump(0.2)
    print(f"footer widths={widths_by_scale}")
    print("OK pending_footer_layout")


CASES = {
    "a": case_a,
    "stop": case_stop,
    "entry": case_entry,
    "ports": case_ports,
    "controller_wiring": case_controller_wiring,
    "b": case_b,
    "c": case_c,
    "d": case_d,
    "e": case_e,
    "f": case_f,
    "toast_round": case_toast_round,
    "press_bounce": case_press_bounce,
    "table_hover": case_table_hover,
    "long_name": case_long_name,
    "abandon_selected": case_abandon_selected,
    "abandon_all": case_abandon_all,
    "pending_footer_layout": case_pending_footer_layout,
    "g": case_g,
    "h": case_h,
    "i": case_i,
    "j": case_j,
    "scan_e2e": case_scan_e2e,
    "scroll_folder": case_scroll_folder,
    "scroll_empty": case_scroll_empty,
    "pending_workbench": case_pending_workbench,
    "doctor_offscreen": case_doctor_offscreen,
    "doctor_pixels": case_doctor_pixels,
}


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "a"
    if name not in CASES:
        raise SystemExit(f"unknown case: {name}")
    app()
    CASES[name]()
    app().processEvents()


if __name__ == "__main__":
    main()
