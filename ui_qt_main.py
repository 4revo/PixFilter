"""PixFilter 的 Qt 主界面（PySide6）——View 层。

职责边界：
- 本模块只构建控件、连接信号、渲染控制器推来的状态；
- 所有业务判定（是否允许选目录、扫描/停止编排、挂起/继续/放弃、撤销、
  Toast 条件、关窗收尾顺序）都在 ``qt_controller.ScanController``；
- 线程约束：``ScanService`` 回调可能来自工作线程，本模块用 ``_relay`` 把
  回调投递到控制器的线程安全队列，再由一个 Qt 定时器在主线程 ``pump``；
  工作线程绝不触碰 QWidget。
"""

from __future__ import annotations

import os
import sys
import webbrowser
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QStatusBar,
    QStyledItemDelegate,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from config import (
    ACCENT,
    ACCENT_HOVER,
    ACCENT_PRESS,
    ACCENT_SOFT,
    AMBER,
    APP_VERSION,
    BG,
    BORDER,
    BUTTON_GAP,
    CARD,
    DISABLED_BG,
    DISABLED_FG,
    FONT_BOLD,
    FONT_SMALL,
    FONT_TITLE,
    FONT_UI,
    GREEN,
    GITHUB_URL,
    HINT_RIGHT_GAP,
    ICON_FILE_NAME,
    LOG_BG,
    LOG_INFO_FG,
    LOG_SKIP_FG,
    LOG_TEXT_PADX,
    LOG_TEXT_PADY,
    MIN_WINDOW_HEIGHT,
    MIN_WINDOW_WIDTH,
    MUTED,
    PAGE_PADX,
    PAGE_PADY,
    PATH_LINK,
    PENDING_ABANDON_ALL_BG,
    PENDING_ABANDON_ALL_CONFIRM_NO,
    PENDING_ABANDON_ALL_CONFIRM_TEXT,
    PENDING_ABANDON_ALL_CONFIRM_TITLE,
    PENDING_ABANDON_ALL_CONFIRM_YES,
    PENDING_ABANDON_ALL_DISABLED_BG,
    PENDING_ABANDON_ALL_DISABLED_FG,
    PENDING_ABANDON_ALL_FG,
    PENDING_ABANDON_ALL_HOVER,
    PENDING_ABANDON_ALL_PRESS,
    PENDING_EMPTY_BG,
    PENDING_EMPTY_PADX,
    PENDING_CELL_PAD_X,
    PENDING_CELL_PAD_Y,
    PENDING_FOOTER_GAP,
    PENDING_INNER_PADX,
    PENDING_INNER_PADY,
    PENDING_ROW_HOVER_BG,
    PENDING_ROW_SELECTED_BG,
    PENDING_ROW_SELECTED_FG,
    PROGRESS_CHUNK_RADIUS,
    PROGRESS_LENGTH,
    PROGRESS_PADDING,
    PROGRESS_RADIUS,
    PROGRESS_SIDE_MARGIN,
    PROGRESS_THICKNESS,
    PROGRESS_TROUGH,
    PURPLE,
    QT_BUTTON_PAD_X,
    QT_BUTTON_PAD_Y,
    QT_BUTTON_RADIUS,
    QT_BUTTON_SINK,
    QT_CONTROLLER_PUMP_MS,
    QT_LOG_FONT_FAMILY,
    QT_LOG_FONT_SIZE,
    QT_LOG_MIN_HEIGHT,
    QT_PENDING_TREE_MIN_HEIGHT,
    QT_STATUS_BAR_HEIGHT,
    QT_TOAST_END_FACTOR,
    QT_TOAST_FADE_MS,
    QT_TOAST_START_FACTOR,
    QT_TOAST_TOP_PAD,
    RED,
    SECONDARY_ACTIVE_BG,
    SECONDARY_DISABLED_BG,
    TEXT,
    THRESHOLD_DEFAULT,
    THRESHOLD_HELP_FG,
    THRESHOLD_MAX,
    THRESHOLD_MIN,
    THRESHOLD_SCALE_LENGTH,
    THRESHOLD_VALUE_WIDTH,
    TOAST_FILL,
    TOAST_FONT_FAMILY,
    TOAST_FONT_SIZE,
    TOAST_HEIGHT,
    TOAST_HOLD_MS,
    TOAST_MAX_WIDTH,
    TOAST_MIN_WIDTH,
    TOAST_PAD_X,
    TOAST_PAD_Y,
    TOAST_RADIUS,
    TOAST_SLIDE_MS,
    TOAST_TEXT,
    UNDO_ALL_TEXT,
    WINDOW_HEIGHT,
    WINDOW_WIDTH,
)
from pixfilter_core import DEFAULT_HASH_SIZE, TRASH_DIR_NAME
from qt_controller import PendingRow, ScanController
from review_qt import PressBounceButton, ensure_qapplication
from review_workbench_qt import QtReviewWorkbench
from scan_service import REVIEW_MODE_BATCH, ScanService


# ---------- 资源与图标 ----------
def resource_path(relative_path: str) -> str:
    """PyInstaller 用 ``_MEIPASS``；源码运行回退到模块所在目录。"""
    bundle_dir = getattr(sys, "_MEIPASS", None)
    if bundle_dir:
        return os.path.abspath(os.path.join(bundle_dir, relative_path))
    return os.path.abspath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), relative_path)
    )


def load_app_icon() -> QIcon:
    """加载多尺寸 ICO；任何失败都返回空 QIcon，不阻断启动。"""
    path = resource_path(ICON_FILE_NAME)
    if not os.path.isfile(path):
        return QIcon()
    try:
        return QIcon(path)
    except Exception:
        return QIcon()


def _font(spec=FONT_UI) -> QFont:
    family = str(spec[0])
    size = int(spec[1])
    bold = len(spec) > 2 and str(spec[2]) == "bold"
    font = QFont(family, size)
    font.setBold(bold)
    return font


_LOG_STYLES: dict[str, tuple[str, bool, bool]] = {
    "info": (LOG_INFO_FG, False, False),
    "group": (ACCENT, True, False),
    "keep": (GREEN, False, False),
    "move": (AMBER, False, False),
    "warn": (AMBER, False, False),
    "skip": (LOG_SKIP_FG, True, False),
    "error": (RED, True, False),
    "summary": (PURPLE, True, False),
    "path_link": (PATH_LINK, False, True),
    "copy_ok": (GREEN, True, False),
}


# ---------- 控件 ----------
class QtButton(PressBounceButton):
    """圆角按钮：QSS 圆角 + hover/pressed/disabled + 手型光标 + 按压回弹。

    按压回弹由 ``PressBounceButton`` 提供：只在 ``paintEvent`` 里缩放绘制内容，
    不改 ``geometry``；因此 QSS 三态颜色照常由 QStyle 绘制，父布局也不会重排。
    """

    def __init__(
        self,
        text: str,
        *,
        primary: bool = False,
        compact: bool = False,
        danger: bool = False,
        parent=None,
    ):
        super().__init__(text, parent)
        self._primary = primary
        self._compact = compact
        self._danger = danger
        self.setFont(_font(FONT_SMALL if compact else FONT_UI))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setStyleSheet(self._style())

    def _style(self) -> str:
        pad_x = 10 if self._compact else QT_BUTTON_PAD_X
        pad_y = 3 if self._compact else QT_BUTTON_PAD_Y
        if self._primary:
            bg, hover, press = ACCENT, ACCENT_HOVER, ACCENT_PRESS
            fg, disabled_bg, disabled_fg = CARD, DISABLED_BG, DISABLED_FG
            border = "border: 0;"
        elif self._danger:
            bg, hover, press = (
                PENDING_ABANDON_ALL_BG,
                PENDING_ABANDON_ALL_HOVER,
                PENDING_ABANDON_ALL_PRESS,
            )
            fg = PENDING_ABANDON_ALL_FG
            disabled_bg = PENDING_ABANDON_ALL_DISABLED_BG
            disabled_fg = PENDING_ABANDON_ALL_DISABLED_FG
            border = "border: 0;"
        else:
            bg, hover, press = CARD, ACCENT_SOFT, SECONDARY_ACTIVE_BG
            fg, disabled_bg, disabled_fg = TEXT, SECONDARY_DISABLED_BG, DISABLED_FG
            border = f"border: 1px solid {BORDER};"
        radius = 6 if self._compact else QT_BUTTON_RADIUS
        return (
            f"QPushButton {{ background-color: {bg}; color: {fg}; {border}"
            f" border-radius: {radius}px; padding: {pad_y}px {pad_x}px; }}"
            f"QPushButton:hover {{ background-color: {hover}; }}"
            f"QPushButton:pressed {{ background-color: {press};"
            f" padding-top: {pad_y + QT_BUTTON_SINK}px;"
            f" padding-bottom: {pad_y - QT_BUTTON_SINK}px; }}"
            f"QPushButton:disabled {{ background-color: {disabled_bg};"
            f" color: {disabled_fg}; }}"
        )

    def changeEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange:
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if self.isEnabled()
                else Qt.CursorShape.ArrowCursor
            )


def _build_abandon_all_box(parent=None) -> QMessageBox:
    """构造「全部放弃」二次确认框（不 exec，便于测试检查文案）。

    ``confirm_abandon_all`` 只负责 exec 并根据点击结果返回布尔值；测试可
    直接替换视图上的 ``confirm_abandon_all``（或本函数）为自动应答，避免
    offscreen 环境被真实模态框卡死。
    """
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(PENDING_ABANDON_ALL_CONFIRM_TITLE)
    box.setText(PENDING_ABANDON_ALL_CONFIRM_TEXT)
    yes = box.addButton(
        PENDING_ABANDON_ALL_CONFIRM_YES, QMessageBox.ButtonRole.AcceptRole
    )
    no = box.addButton(
        PENDING_ABANDON_ALL_CONFIRM_NO, QMessageBox.ButtonRole.RejectRole
    )
    box.setDefaultButton(no)
    box.setEscapeButton(no)
    box._pixfilter_yes_button = yes
    return box


class _PendingTableDelegate(QStyledItemDelegate):
    """待处理表格单元格：整行 hover 底色 + 选中优先 + 单行省略号。

    悬停行只保存在 ``PendingTable`` 里，paint 时读取；不逐格 ``setBackground``，
    因此不会闪烁也不会残留。颜色全部来自 ``config``。
    """

    def __init__(self, table: "PendingTable", parent=None):
        super().__init__(parent)
        self._table = table
        # 测试/诊断用：最近一次真实绘制出的文本（已省略）。
        self.drawn: dict[tuple[int, int], str] = {}

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = (not selected) and index.row() == self._table.hovered_row()
        if selected:
            background = PENDING_ROW_SELECTED_BG
            foreground = PENDING_ROW_SELECTED_FG
        elif hovered:
            background = PENDING_ROW_HOVER_BG
            foreground = TEXT
        else:
            background = CARD
            foreground = TEXT

        painter.save()
        painter.fillRect(option.rect, QColor(background))
        font = option.font
        painter.setFont(font)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        available = max(1, option.rect.width() - 2 * PENDING_CELL_PAD_X)
        drawn = QFontMetrics(font).elidedText(
            text, Qt.TextElideMode.ElideRight, available
        )
        self.drawn[(index.row(), index.column())] = drawn
        painter.setPen(QColor(foreground))
        painter.drawText(
            option.rect.adjusted(
                PENDING_CELL_PAD_X, PENDING_CELL_PAD_Y,
                -PENDING_CELL_PAD_X, -PENDING_CELL_PAD_Y,
            ),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            drawn,
        )
        painter.restore()


class PendingTable(QTableWidget):
    """待处理表格：整行 hover 高亮，只对旧行/新行做 ``viewport().update(rect)``。"""

    def __init__(self, rows: int, columns: int, parent=None):
        super().__init__(rows, columns, parent)
        self._hover_row = -1
        # 测试/诊断用：每次局部重绘请求的矩形（可用于证明没有全表 update）。
        self.update_rects: list[QRect] = []
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setItemDelegate(_PendingTableDelegate(self, self))

    def hovered_row(self) -> int:
        return self._hover_row

    def clear_update_records(self) -> None:
        self.update_rects.clear()

    def _row_rect(self, row: int) -> QRect:
        return QRect(
            0,
            self.rowViewportPosition(row),
            self.viewport().width(),
            self.rowHeight(row),
        )

    def set_hover_row(self, row: int) -> None:
        if row == self._hover_row:
            return
        old = self._hover_row
        self._hover_row = row
        for candidate in (old, row):
            if 0 <= candidate < self.rowCount():
                rect = self._row_rect(candidate)
                if not rect.isEmpty():
                    self.update_rects.append(rect)
                    self.viewport().update(rect)

    def setRowCount(self, rows: int) -> None:  # noqa: N802 (Qt 命名)
        super().setRowCount(rows)
        if self._hover_row >= rows:
            self._hover_row = -1

    def viewportEvent(self, event) -> bool:  # noqa: N802 (Qt 命名)
        kind = event.type()
        if kind == QEvent.Type.MouseMove:
            index = self.indexAt(event.position().toPoint())
            self.set_hover_row(index.row() if index.isValid() else -1)
        elif kind == QEvent.Type.Leave:
            self.set_hover_row(-1)
        return super().viewportEvent(event)


class ElidedLabel(QLabel):
    """单行提示：完整文案保存在 ``full_text``，超宽时以 ``…`` 截断。"""

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self._full_text = str(text)
        self.setWordWrap(False)
        self.setMinimumWidth(1)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    @property
    def full_text(self) -> str:
        return self._full_text

    def set_full_text(self, text: str) -> None:
        self._full_text = str(text)
        self.refresh()

    def refresh(self) -> None:
        metrics = QFontMetrics(self.font())
        self.setText(
            metrics.elidedText(
                self._full_text,
                Qt.TextElideMode.ElideRight,
                max(1, self.width()),
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        self.refresh()
        super().resizeEvent(event)

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        height = super().minimumSizeHint().height()
        return QSize(1, height)


class CopyableLog(QTextBrowser):
    """等宽只读日志；点击“当前扫描目录”行会发出路径复制信号。"""

    pathClicked = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFont(QFont(QT_LOG_FONT_FAMILY, QT_LOG_FONT_SIZE))
        self.setStyleSheet(
            f"QTextBrowser {{ background-color: {LOG_BG}; color: {TEXT};"
            f" border: 0; padding: {LOG_TEXT_PADY}px {LOG_TEXT_PADX}px; }}"
        )
        self.setMinimumHeight(QT_LOG_MIN_HEIGHT)
        self._line_count = 0
        self._link_blocks: dict[int, Path] = {}

    def append_line(
        self,
        message: str,
        *,
        color: str = LOG_INFO_FG,
        bold: bool = False,
        underline: bool = False,
        link_path: Optional[Path] = None,
    ) -> int:
        cursor = QTextCursor(self.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if self._line_count > 0:
            cursor.insertBlock()
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        font = QFont(QT_LOG_FONT_FAMILY, QT_LOG_FONT_SIZE)
        font.setBold(bold)
        fmt.setFont(font)
        fmt.setFontUnderline(underline)
        cursor.insertText(message, fmt)
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        self._line_count += 1
        block = cursor.blockNumber()
        if link_path is not None:
            self._link_blocks[block] = Path(link_path)
        return block

    def clear_log(self) -> None:
        self.clear()
        self._line_count = 0
        self._link_blocks.clear()

    def copy_block(self, block_number: int) -> bool:
        path = self._link_blocks.get(int(block_number))
        if path is None:
            return False
        self.pathClicked.emit(str(path))
        return True

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            cursor = self.cursorForPosition(event.position().toPoint())
            if self.copy_block(cursor.blockNumber()):
                event.accept()
                return
        super().mousePressEvent(event)


class QtToast(QWidget):
    """主窗口顶部居中的非阻塞 Toast：滑入 → 停留 → 原地 windowOpacity 淡出。"""

    def __init__(self, parent: QWidget, text: str):
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.NoDropShadowWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setObjectName("toastRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        # 顶层必须逐像素透明：圆角外的四角不画任何底色。
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.setAutoFillBackground(False)
        self.setWindowIcon(parent.window().windowIcon())
        self.setWindowOpacity(QT_TOAST_START_FACTOR)
        # 顶层只声明透明，绝不在这里画背景/边框，圆角完全交给 #toastFrame。
        self.setStyleSheet(
            "QWidget#toastRoot { background: transparent; border: none; }"
        )

        self._frame = QFrame(self)
        self._frame.setObjectName("toastFrame")
        self._frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._frame.setStyleSheet(
            f"QFrame#toastFrame {{ background-color: {TOAST_FILL};"
            f" border: none; border-radius: {TOAST_RADIUS}px; }}"
        )

        label_font = QFont(TOAST_FONT_FAMILY, TOAST_FONT_SIZE)
        metrics = QFontMetrics(label_font)
        # Qt 的控件坐标是逻辑像素，物理像素会按 DPR 自动放大，
        # 因此 300 逻辑像素在 2x 屏上就是 600 物理像素，不能再乘一次 DPR。
        max_width = TOAST_MAX_WIDTH
        max_text = max(1, max_width - 2 * TOAST_PAD_X)
        shown = metrics.elidedText(str(text), Qt.TextElideMode.ElideRight, max_text)
        width = max(
            TOAST_MIN_WIDTH,
            metrics.horizontalAdvance(shown) + 2 * TOAST_PAD_X,
        )
        width = min(width, max_width)
        height = max(TOAST_HEIGHT, metrics.height() + 2 * TOAST_PAD_Y)

        self._label = QLabel(shown, self._frame)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setFont(label_font)
        self._label.setMinimumWidth(1)
        self._label.setWordWrap(False)
        self._label.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self._label.setStyleSheet(
            f"QLabel {{ background: transparent; border: none;"
            f" color: {TOAST_TEXT}; }}"
        )

        # 顶层布局零边距，避免在窗口外圈露出任何不透明区域。
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._frame)

        inner = QHBoxLayout(self._frame)
        inner.setContentsMargins(
            TOAST_PAD_X, TOAST_PAD_Y, TOAST_PAD_X, TOAST_PAD_Y
        )
        inner.setSpacing(0)
        inner.addWidget(self._label)

        self.resize(width, height)

        main = parent.window()
        geo = main.geometry()
        center_x = geo.x() + max(0, (geo.width() - width) // 2)
        self._end_pos = QPoint(center_x, geo.y() + QT_TOAST_TOP_PAD)
        self._start_pos = QPoint(center_x, geo.y() - height)
        self.move(self._start_pos)
        self.show()

        self._slide = QPropertyAnimation(self, b"pos", self)
        self._slide.setDuration(TOAST_SLIDE_MS)
        self._slide.setStartValue(self._start_pos)
        self._slide.setEndValue(self._end_pos)
        self._slide.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._slide.finished.connect(self._start_hold)

        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(QT_TOAST_FADE_MS)
        self._fade.setStartValue(QT_TOAST_START_FACTOR)
        self._fade.setEndValue(QT_TOAST_END_FACTOR)
        self._fade.finished.connect(self.close)

        self._hold = QTimer(self)
        self._hold.setSingleShot(True)
        self._hold.setInterval(TOAST_HOLD_MS)
        self._hold.timeout.connect(self._start_fade)

        self._slide.start()

    def _start_hold(self) -> None:
        self.move(self._end_pos)
        self._hold.start()

    def _start_fade(self) -> None:
        # 原地淡出：位置固定，仅动画 windowOpacity。
        self.move(self._end_pos)
        self._fade.start()

    def closeEvent(self, event) -> None:  # noqa: N802
        for animation in (getattr(self, "_slide", None), getattr(self, "_fade", None)):
            if animation is not None:
                animation.stop()
        if hasattr(self, "_hold"):
            self._hold.stop()
        event.accept()


# ---------- 主窗口（View） ----------
class PixFilterQtWindow(QMainWindow):
    """只做布局、渲染与转发；所有判定都在 ``qt_controller.ScanController``。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"PixFilter v{APP_VERSION} · 照片去重")
        self.setMinimumSize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)
        self.setWindowIcon(load_app_icon())

        self._closed = False
        self._closing = False
        self._toast: Optional[QtToast] = None
        self._workbench: Optional[QtReviewWorkbench] = None
        self._syncing_selection = False
        self._controller: Optional[ScanController] = None

        self._service = ScanService(
            progress_cb=self._relay("on_progress"),
            log_cb=self._relay("on_log"),
            group_ready_cb=self._relay("on_group_ready"),
            groups_ready_cb=self._relay("on_groups_ready"),
            suspended_cb=self._relay("on_suspended"),
            finished_cb=self._relay("on_finished"),
            review_finished_cb=self._relay("on_review_finished"),
            undo_finished_cb=self._relay("on_undo_finished"),
            error_cb=self._relay("on_error"),
            review_mode=REVIEW_MODE_BATCH,
        )

        self._build_ui()
        self._controller = ScanController(self._service, self)
        self._wire_signals()

        self.append_log(
            "info",
            "PixFilter 已就绪。选择文件夹后点击“开始扫描”。\n"
            "完全重复按 MD5 识别；相似图片按 pHash 识别。\n"
            "发现分组后会一次性打开批量复核工作台；点“跳过”= 放弃该组，"
            "点 X = 未处理组全部挂起并留在“待处理重复组”。\n"
            f"只有点确认，才会把多余图片移入“{TRASH_DIR_NAME}”。",
        )
        self._controller.start()

        # 唯一的 Qt 定时器职责：把控制器跨线程队列抽到主线程。
        self._pump_timer = QTimer(self)
        self._pump_timer.setInterval(QT_CONTROLLER_PUMP_MS)
        self._pump_timer.timeout.connect(self._controller.pump)
        self._pump_timer.start()

    # ---------- 服务回调 → 控制器队列 ----------
    @property
    def controller(self) -> ScanController:
        """控制器单实例（只读引用，便于接线与测试）。"""
        return self._controller

    def _relay(self, name: str):
        def callback(*args) -> None:
            controller = self._controller
            if controller is not None:
                controller.post(name, *args)

        return callback

    # ---------- 布局 ----------
    def _build_ui(self) -> None:
        self.setStyleSheet(f"QMainWindow {{ background-color: {BG}; }}")
        central = QWidget(self)
        central.setObjectName("centralRoot")
        # 用 ID 选择器，避免裸规则级联到滚动条等子控件。
        central.setStyleSheet(
            f"QWidget#centralRoot {{ background-color: {BG}; }}"
        )
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(PAGE_PADX, PAGE_PADY, PAGE_PADX, PAGE_PADY)
        outer.setSpacing(10)

        header = QHBoxLayout()
        title = QLabel("PixFilter 照片去重")
        title.setFont(_font(FONT_TITLE))
        title.setStyleSheet(f"color: {TEXT};")
        header.addWidget(title)
        subtitle = QLabel("纯本地运行 · 精确重复 + 视觉相似 · 只移动，不删除")
        subtitle.setFont(_font(FONT_SMALL))
        subtitle.setStyleSheet(f"color: {MUTED};")
        header.addWidget(subtitle, 0, Qt.AlignmentFlag.AlignBottom)
        header.addStretch(1)
        outer.addLayout(header)

        # 1) 顶部一行：选择目录 / 撤销 / 打开重复目录
        folder_row = QHBoxLayout()
        folder_row.setSpacing(BUTTON_GAP)
        self.btn_choose_folder = QtButton("点击此处选择扫描文件夹", parent=central)
        self.btn_undo = QtButton(UNDO_ALL_TEXT, parent=central)
        self.btn_open_trash = QtButton("打开重复目录", parent=central)
        self.btn_undo.setEnabled(False)
        self.btn_open_trash.setEnabled(True)
        for button in (
            self.btn_choose_folder,
            self.btn_undo,
            self.btn_open_trash,
        ):
            folder_row.addWidget(button)
        folder_row.addStretch(1)
        outer.addLayout(folder_row)
        self.folder_row = folder_row

        # 2) 阈值 + 开始/停止同一水平带
        action_row = QHBoxLayout()
        action_row.setSpacing(BUTTON_GAP)
        threshold_box = QVBoxLayout()
        threshold_box.setSpacing(2)
        threshold_row = QHBoxLayout()
        threshold_row.setSpacing(10)
        threshold_label = QLabel("相似阈值")
        threshold_label.setFont(_font(FONT_BOLD))
        threshold_label.setStyleSheet(f"color: {TEXT};")
        threshold_row.addWidget(threshold_label)

        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setRange(THRESHOLD_MIN, THRESHOLD_MAX)
        self.threshold_slider.setValue(int(THRESHOLD_DEFAULT))
        self.threshold_slider.setFixedWidth(THRESHOLD_SCALE_LENGTH)
        self.threshold_slider.setPageStep(1)
        self.threshold_slider.setSingleStep(1)
        self.threshold_slider.setCursor(Qt.CursorShape.PointingHandCursor)
        self.threshold_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ height: 4px; background: {ACCENT_SOFT};"
            f" border-radius: 2px; }}"
            f"QSlider::sub-page:horizontal {{ background: {ACCENT};"
            f" border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {ACCENT}; width: 14px;"
            f" margin: -5px 0; border-radius: 7px; }}"
        )
        threshold_row.addWidget(self.threshold_slider)

        self.threshold_value_label = QLabel(f"当前: {int(THRESHOLD_DEFAULT)}", central)
        self.threshold_value_label.setFont(_font(FONT_UI))
        self.threshold_value_label.setStyleSheet(f"color: {TEXT};")
        self.threshold_value_label.setFixedWidth(THRESHOLD_VALUE_WIDTH * 9)
        threshold_row.addWidget(self.threshold_value_label)
        threshold_row.addStretch(1)
        threshold_box.addLayout(threshold_row)

        self.threshold_help_label = QLabel(
            f"（pHash {DEFAULT_HASH_SIZE}x{DEFAULT_HASH_SIZE}，"
            f"默认 {THRESHOLD_DEFAULT}；越低越严格）",
            central,
        )
        self.threshold_help_label.setFont(_font(FONT_SMALL))
        self.threshold_help_label.setStyleSheet(f"color: {THRESHOLD_HELP_FG};")
        threshold_box.addWidget(self.threshold_help_label)
        action_row.addLayout(threshold_box, 1)

        self.btn_stop = QtButton("停止", parent=central)
        self.btn_start = QtButton("开始扫描", primary=True, parent=central)
        self.btn_stop.setEnabled(False)
        action_row.addWidget(self.btn_start, 0, Qt.AlignmentFlag.AlignTop)
        action_row.addWidget(self.btn_stop, 0, Qt.AlignmentFlag.AlignTop)
        outer.addLayout(action_row)

        # 3) 日志卡片
        log_card = self._card(central)
        log_layout = QVBoxLayout(log_card)
        log_layout.setContentsMargins(LOG_TEXT_PADX, 10, LOG_TEXT_PADX, 8)
        log_layout.setSpacing(6)
        log_head = QHBoxLayout()
        log_title = QLabel("扫描日志")
        log_title.setFont(_font(FONT_BOLD))
        log_title.setStyleSheet(f"color: {TEXT};")
        log_head.addWidget(log_title)
        log_head.addStretch(1)
        log_hint = QLabel(f"保留与确认后移入“{TRASH_DIR_NAME}”的路径都会记录在这里")
        log_hint.setFont(_font(FONT_SMALL))
        log_hint.setStyleSheet(f"color: {MUTED};")
        log_head.addWidget(log_hint)
        log_layout.addLayout(log_head)

        self.log_view = CopyableLog(log_card)
        self.log_view.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.log_view.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.log_view.pathClicked.connect(self._copy_path)
        log_layout.addWidget(self.log_view, 1)
        outer.addWidget(log_card, 1)

        # 4) 待处理重复组面板
        pending_card = self._card(central)
        pending_layout = QVBoxLayout(pending_card)
        pending_layout.setContentsMargins(
            PENDING_INNER_PADX, PENDING_INNER_PADY,
            PENDING_INNER_PADX, PENDING_INNER_PADY,
        )
        pending_layout.setSpacing(8)
        pending_head = QHBoxLayout()
        pending_title = QLabel("待处理重复组")
        pending_title.setFont(_font(FONT_BOLD))
        pending_title.setStyleSheet(f"color: {TEXT};")
        pending_head.addWidget(pending_title)
        pending_hint = QLabel("复核工作台点 X 会挂起到这里，可稍后继续处理或放弃")
        pending_hint.setFont(_font(FONT_SMALL))
        pending_hint.setStyleSheet(f"color: {MUTED};")
        pending_head.addWidget(pending_hint)
        pending_head.addStretch(1)
        self.pending_count_label = QLabel("")
        self.pending_count_label.setFont(_font(FONT_SMALL))
        self.pending_count_label.setStyleSheet(f"color: {AMBER};")
        pending_head.addWidget(self.pending_count_label)
        pending_layout.addLayout(pending_head)

        self.pending_stack = QStackedWidget(pending_card)
        self.pending_stack.setObjectName("pendingStack")
        self.pending_stack.setContentsMargins(0, 0, 0, 0)
        # 容器自身不能再画边框：卡片边框已经由外层 QFrame#cardFrame 负责。
        self.pending_stack.setStyleSheet(
            "QStackedWidget#pendingStack { border: none;"
            " background-color: transparent; }"
        )
        self.pending_stack.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        self.pending_empty = QLabel("暂无待处理组 · 复核工作台点 X 会挂起到这里")
        self.pending_empty.setFont(_font(FONT_SMALL))
        self.pending_empty.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.pending_empty.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.pending_empty.setStyleSheet(
            f"QLabel {{ background-color: {PENDING_EMPTY_BG}; color: {MUTED};"
            f" border: none; border-radius: 6px; }}"
        )
        # 内边距改用 indent，避免 padding 让 contentsMargins 变成非 0。
        self.pending_empty.setIndent(PENDING_EMPTY_PADX)
        self.pending_empty.setContentsMargins(0, 0, 0, 0)
        self.pending_table = PendingTable(0, 5)
        self.pending_table.setHorizontalHeaderLabels(
            ["组号", "类型", "张数", "概要", "状态"]
        )
        self.pending_table.verticalHeader().setVisible(False)
        self.pending_table.setShowGrid(False)
        # 去掉表格自身边框/外框：面板描边只由外层卡片负责，避免右缘多出 1px 灰线。
        self.pending_table.setFrameShape(QFrame.Shape.NoFrame)
        self.pending_table.setLineWidth(0)
        self.pending_table.setMidLineWidth(0)
        self.pending_table.setViewportMargins(0, 0, 0, 0)
        # 视口是普通 QWidget，没有 frameShape；用零边距保证内容不产生内框。
        self.pending_table.viewport().setContentsMargins(0, 0, 0, 0)
        # 整行选中、单选、不可编辑；焦点只落在表格内部，不画虚线框。
        self.pending_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.pending_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.pending_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.pending_table.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.pending_table.setMinimumHeight(QT_PENDING_TREE_MIN_HEIGHT)
        # 需要滚动时才出现；配合透明槽，空态/不溢出时右侧不留灰条。
        self.pending_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.pending_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.pending_table.setStyleSheet(
            f"QTableWidget {{ background-color: {CARD}; color: {TEXT};"
            f" border: none; gridline-color: transparent; outline: 0; }}"
            f"QTableWidget::item {{ padding: 2px 4px; }}"
            f"QTableWidget::item:selected {{"
            f" background-color: {PENDING_ROW_SELECTED_BG};"
            f" color: {PENDING_ROW_SELECTED_FG}; }}"
            f"QHeaderView {{ border: none; background-color: transparent; }}"
            f"QHeaderView::section {{ background-color: {PENDING_EMPTY_BG};"
            f" color: {TEXT}; border: none; padding: 4px; }}"
            f"QTableCornerButton::section {{ background-color: transparent;"
            f" border: none; }}"
        )
        header = self.pending_table.horizontalHeader()
        header.setFrameShape(QFrame.Shape.NoFrame)
        header.setHighlightSections(False)
        for column, width in enumerate((70, 90, 60, 300, 80)):
            self.pending_table.setColumnWidth(column, width)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.pending_stack.addWidget(self.pending_empty)
        self.pending_stack.addWidget(self.pending_table)
        self.pending_stack.setCurrentWidget(self.pending_empty)
        pending_layout.addWidget(self.pending_stack)

        pending_footer = QHBoxLayout()
        pending_footer.setSpacing(PENDING_FOOTER_GAP)
        pending_footer_hint = QLabel("选中一行后可继续处理或放弃；双击一行直接打开批量复核工作台。")
        pending_footer_hint.setFont(_font(FONT_SMALL))
        pending_footer_hint.setStyleSheet(f"color: {MUTED};")
        pending_footer.addWidget(pending_footer_hint, 1)
        # 顺序（从左到右）：全部放弃 | 放弃处理选中组 | 继续处理。
        # 危险操作靠左、主操作靠右，降低误触概率。
        self.btn_pending_abandon_all = QtButton(
            "全部放弃", danger=True, parent=pending_card
        )
        self.btn_pending_abandon = QtButton(
            "放弃处理选中组", parent=pending_card
        )
        self.btn_pending_continue = QtButton(
            "继续处理", primary=True, parent=pending_card
        )
        self.btn_pending_abandon_all.setEnabled(False)
        self.btn_pending_abandon.setEnabled(False)
        self.btn_pending_continue.setEnabled(False)
        self.btn_pending_abandon_all.setToolTip(
            "仅在有待处理组且未扫描/未打开批量复核工作台时可用"
        )
        self.btn_pending_abandon.setToolTip("请先在待处理列表选择一组")
        for button in (
            self.btn_pending_abandon_all,
            self.btn_pending_abandon,
            self.btn_pending_continue,
        ):
            pending_footer.addWidget(
                button, 0, Qt.AlignmentFlag.AlignVCenter
            )
        pending_layout.addLayout(pending_footer)
        self.pending_footer = pending_footer
        self.pending_footer_hint = pending_footer_hint
        outer.addWidget(pending_card)

        # 5) 底部提示行 + GitHub
        hint_row = QHBoxLayout()
        hint_row.setSpacing(HINT_RIGHT_GAP)
        self.hint_text = (
            "提示：扫描只处理 .jpg / .jpeg / .png；只有逐组确认后才会移动，"
            "且保留原相对路径、重名自动加后缀。"
        )
        self.hint_label = ElidedLabel(self.hint_text, central)
        self.hint_label.setFont(_font(FONT_SMALL))
        self.hint_label.setStyleSheet(f"color: {MUTED};")
        hint_row.addWidget(self.hint_label, 1)
        self.btn_github = QtButton("GitHub 项目地址", compact=True, parent=central)
        hint_row.addWidget(self.btn_github, 0, Qt.AlignmentFlag.AlignRight)
        outer.addLayout(hint_row)
        self.hint_row = hint_row

        # 6) 状态栏：状态文字 + 两阶段进度条
        status_bar = QStatusBar(self)
        status_bar.setSizeGripEnabled(False)
        status_bar.setFixedHeight(QT_STATUS_BAR_HEIGHT)
        status_bar.setStyleSheet(
            f"QStatusBar {{ background-color: {BG}; color: {MUTED}; }}"
        )
        self.setStatusBar(status_bar)
        self.status_label = QLabel("就绪 · 请选择要扫描的文件夹")
        self.status_label.setFont(_font(FONT_SMALL))
        self.status_label.setStyleSheet(f"color: {MUTED};")
        self.status_label.setMinimumWidth(1)
        status_bar.addWidget(self.status_label, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(PROGRESS_LENGTH)
        self.progress.setFixedHeight(PROGRESS_THICKNESS)
        self.progress.setStyleSheet(
            f"QProgressBar {{ background-color: {PROGRESS_TROUGH};"
            f" border: none; border-radius: {PROGRESS_RADIUS}px;"
            f" padding: {PROGRESS_PADDING}px; }}"
            f"QProgressBar::chunk {{ background-color: {ACCENT};"
            f" border-radius: {PROGRESS_CHUNK_RADIUS}px; }}"
        )
        self.progress.setVisible(False)
        # 状态栏右侧常驻版本号：版本只在 config.APP_VERSION 定义，避免散落硬编码。
        self.version_label = QLabel(f"v{APP_VERSION}")
        self.version_label.setFont(_font(FONT_SMALL))
        self.version_label.setStyleSheet(f"color: {MUTED};")
        status_bar.addPermanentWidget(self.version_label)
        # 左右各留 PROGRESS_SIDE_MARGIN：进度条不贴窗口右缘，也与之左侧内容保持间距。
        progress_holder = QWidget()
        progress_holder.setObjectName("progressHolder")
        progress_layout = QHBoxLayout(progress_holder)
        progress_layout.setContentsMargins(
            PROGRESS_SIDE_MARGIN, 0, PROGRESS_SIDE_MARGIN, 0
        )
        progress_layout.setSpacing(0)
        progress_layout.addWidget(self.progress)
        status_bar.addPermanentWidget(progress_holder)

    def _wire_signals(self) -> None:
        """把控件信号接到控制器的意图方法（视图只做转发）。"""
        controller = self._controller
        self.btn_choose_folder.clicked.connect(controller.on_choose_folder_clicked)
        self.btn_undo.clicked.connect(controller.on_undo_clicked)
        self.btn_open_trash.clicked.connect(controller.on_open_trash_clicked)
        self.threshold_slider.valueChanged.connect(controller.on_threshold_changed)
        self.btn_start.clicked.connect(controller.on_start_clicked)
        self.btn_stop.clicked.connect(controller.on_stop_clicked)
        # clicked 会附带 bool，必须用 lambda 吸收，避免被当成组号。
        self.btn_pending_continue.clicked.connect(
            lambda _checked=False: controller.on_resume_pending()
        )
        self.btn_pending_abandon.clicked.connect(
            lambda _checked=False: controller.on_abandon_pending()
        )
        self.btn_pending_abandon_all.clicked.connect(
            lambda _checked=False: controller.on_abandon_all_pending()
        )
        self.btn_github.clicked.connect(controller.on_github_clicked)
        self.pending_table.itemSelectionChanged.connect(
            self._on_selection_changed
        )
        self.pending_table.cellDoubleClicked.connect(
            self._on_cell_double_clicked
        )

    def _card(self, parent: QWidget) -> QFrame:
        """卡片容器：只用 ID 选择器，避免 QFrame 规则层叠到 QLabel/QStackedWidget。"""
        card = QFrame(parent)
        card.setObjectName("cardFrame")
        card.setStyleSheet(
            f"QFrame#cardFrame {{ background-color: {CARD};"
            f" border: 1px solid {BORDER}; border-radius: 8px; }}"
        )
        return card

    # ---------- 日志（渲染） ----------
    def append_log(self, level: str, text: str) -> None:
        if self._closed:
            return
        color, bold, underline = _LOG_STYLES.get(level, _LOG_STYLES["info"])
        for line in str(text).rstrip().splitlines() or [""]:
            self.log_view.append_line(
                line, color=color, bold=bold, underline=underline
            )

    def append_copyable_path(self, path: Path) -> None:
        if self._closed:
            return
        self.log_view.append_line(
            f"当前扫描目录：{path}（左键点击复制）",
            color=PATH_LINK,
            underline=True,
            link_path=path,
        )

    def clear_log(self) -> None:
        if self._closed:
            return
        self.log_view.clear_log()

    def _copy_path(self, path: str) -> None:
        if self._closed:
            return
        try:
            QApplication.clipboard().setText(str(path))
        except Exception:
            return
        self.append_log("copy_ok", "✅ 路径已复制到剪贴板")

    # ---------- 端口：按钮 / 阈值 / 状态 ----------
    def set_choose_folder_enabled(self, enabled: bool, reason_text: str) -> None:
        self.btn_choose_folder.setEnabled(enabled)
        self.btn_choose_folder.setToolTip(reason_text)

    def set_scan_buttons(
        self, scanning: bool, can_stop: bool, can_start: bool
    ) -> None:
        was_visible = self.progress.isVisible()
        self.btn_start.setEnabled(can_start)
        self.btn_stop.setEnabled(can_stop)
        self.threshold_slider.setEnabled(not scanning)
        if scanning and not was_visible:
            self.progress.setValue(0)
        self.progress.setVisible(scanning)

    def set_threshold(self, value: int) -> None:
        self.threshold_slider.blockSignals(True)
        self.threshold_slider.setValue(int(value))
        self.threshold_slider.blockSignals(False)
        self.threshold_value_label.setText(f"当前: {int(value)}")

    def get_threshold(self) -> int:
        return max(THRESHOLD_MIN, min(THRESHOLD_MAX, int(self.threshold_slider.value())))

    def set_status(self, text: str, color: str = MUTED) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")

    def show_progress(self, stage: str, done: int, total: int) -> None:
        if self._closed:
            return
        fraction = (done / total) if total > 0 else 0.0
        value = (
            int(round(fraction * 50))
            if stage == "md5"
            else 50 + int(round(fraction * 50))
        )
        self.progress.setValue(min(100, max(0, value)))

    def hide_progress(self) -> None:
        self.progress.setVisible(False)

    def set_pending_buttons(
        self,
        continue_enabled: bool,
        abandon_enabled: bool,
        abandon_all_enabled: bool,
    ) -> None:
        self.btn_pending_continue.setEnabled(continue_enabled)
        self.btn_pending_abandon.setEnabled(abandon_enabled)
        self.btn_pending_abandon_all.setEnabled(abandon_all_enabled)
        self.btn_pending_abandon.setToolTip(
            "放弃当前选中的一组，不移动任何文件"
            if abandon_enabled
            else "请先在待处理列表选择一组"
        )
        self.btn_pending_abandon_all.setToolTip(
            "放弃所有待处理组（含挂起组），不移动任何文件"
            if abandon_all_enabled
            else "仅在有待处理组且未扫描/未打开批量复核工作台时可用"
        )

    def confirm_abandon_all(self) -> bool:
        """「全部放弃」二次确认；测试可整体替换本方法实现自动应答。"""
        box = _build_abandon_all_box(self)
        box.exec()
        return box.clickedButton() is getattr(
            box, "_pixfilter_yes_button", None
        )

    def set_undo_enabled(self, enabled: bool) -> None:
        self.btn_undo.setEnabled(enabled)

    def set_github_available(self) -> None:
        self.btn_github.setEnabled(True)

    # ---------- 端口：待处理面板（纯渲染） ----------
    def _selected_key(self) -> Optional[int]:
        selected = self.pending_table.selectedItems()
        if not selected:
            return None
        item = self.pending_table.item(selected[0].row(), 0)
        if item is None:
            return None
        key = item.data(Qt.ItemDataRole.UserRole)
        return int(key) if key is not None else None

    def _on_selection_changed(self) -> None:
        if self._syncing_selection or self._closed:
            return
        controller = self._controller
        if controller is None:
            return
        key = self._selected_key()
        controller.on_pending_selection_changed([] if key is None else [key])

    def _on_cell_double_clicked(self, row: int, _column: int) -> None:
        controller = self._controller
        if controller is None:
            return
        item = self.pending_table.item(row, 0)
        key = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        controller.on_resume_pending(int(key) if key is not None else None)

    def refresh_pending(self, rows: list[PendingRow]) -> None:
        if self._closed:
            return
        current = self._selected_key()
        selected = [row.key for row in rows if row.selected]
        if current in selected:
            selected_key = current
        elif selected:
            selected_key = selected[0]
        else:
            selected_key = None

        self.pending_table.setRowCount(len(rows))
        target_row = -1
        for position, row in enumerate(rows):
            values = [row.label, row.kind, row.total, row.summary, row.status]
            for column, value in enumerate(values):
                item = self.pending_table.item(position, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.pending_table.setItem(position, column, item)
                item.setText(str(value))
                item.setFlags(
                    Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                )
            first = self.pending_table.item(position, 0)
            first.setData(Qt.ItemDataRole.UserRole, row.key)
            if row.key == selected_key:
                target_row = position

        self._syncing_selection = True
        try:
            self.pending_table.clearSelection()
            if target_row >= 0:
                self.pending_table.setCurrentCell(target_row, 0)
                self.pending_table.selectRow(target_row)
            else:
                self.pending_table.setCurrentCell(-1, -1)
        finally:
            self._syncing_selection = False

        self.pending_count_label.setText(f"共 {len(rows)} 组" if rows else "")
        self.pending_stack.setCurrentWidget(
            self.pending_table if rows else self.pending_empty
        )

    # ---------- 端口：工作台 ----------
    def show_workbench(
        self, groups: list, root: Path, initial_index: int = 0
    ) -> None:
        workbench = QtReviewWorkbench(
            list(groups),
            Path(root),
            log=self._controller.on_log,
            decide=self._controller.on_group_decided,
            undo_group=self._controller.on_group_undo,
            parent=self,
            initial_index=int(initial_index),
        )
        workbench.setWindowIcon(self.windowIcon())
        workbench.closed.connect(self._on_workbench_closed)
        self._workbench = workbench
        workbench.show()

    def focus_workbench(self, index: int) -> None:
        if self._workbench is not None:
            self._workbench.focus_group(int(index))

    def close_workbench(self) -> None:
        workbench = self._workbench
        self._workbench = None
        if workbench is not None:
            try:
                workbench.discard()
                workbench.deleteLater()
            except Exception:
                pass

    def is_workbench_open(self) -> bool:
        return self._workbench is not None

    def _on_workbench_closed(self) -> None:
        if self._workbench is None:
            # close_workbench() 已主动清理并通知控制器。
            return
        workbench = self._workbench
        self._workbench = None
        workbench.deleteLater()
        if self._closed:
            return
        self._controller.on_workbench_closed()

    # ---------- 端口：提示 / 平台调用 ----------
    def show_toast(self, text: str) -> None:
        if self._closed:
            return
        if self._toast is not None:
            self._toast.close()
        self._toast = QtToast(self, text)

    def show_error(self, text: str) -> None:
        QMessageBox.critical(self, "PixFilter", text)

    def show_info(self, title: str, text: str) -> None:
        QMessageBox.information(self, title, text)

    def show_warning(self, title: str, text: str) -> None:
        QMessageBox.warning(self, title, text)

    def select_folder(self, start_dir: Path) -> Optional[Path]:
        selected = QFileDialog.getExistingDirectory(
            self, "选择要扫描的文件夹", str(start_dir)
        )
        return Path(selected) if selected else None

    def open_folder(self, path: Path) -> None:
        os.startfile(str(path))  # type: ignore[attr-defined]

    def open_github_url(self) -> None:
        webbrowser.open(GITHUB_URL)

    # ---------- 关闭 ----------
    def closeEvent(self, event) -> None:  # noqa: N802
        if self._closing:
            event.accept()
            return
        self._closing = True
        self._closed = True

        # 控制器负责顺序：取消服务 → 关工作台 → 关弹窗。
        self._controller.on_close_requested()
        self._pump_timer.stop()

        toast = self._toast
        self._toast = None
        if toast is not None:
            toast.close()

        for timer in self.findChildren(QTimer):
            timer.stop()

        event.accept()
        app = QApplication.instance()
        if app is not None:
            QTimer.singleShot(0, app.quit)


def run_qt_app() -> int:
    """创建唯一 QApplication 与主窗口，进入 Qt 主事件循环。"""
    app = ensure_qapplication()
    app.setWindowIcon(load_app_icon())
    window = PixFilterQtWindow()
    window.show()
    return app.exec()


__all__ = [
    "PixFilterQtWindow",
    "QtToast",
    "ElidedLabel",
    "CopyableLog",
    "QtButton",
    "load_app_icon",
    "resource_path",
    "run_qt_app",
]
