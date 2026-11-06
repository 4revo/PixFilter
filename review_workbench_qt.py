"""PixFilter 的 Qt 批处理复核工作台（左右分栏）。

设计要点：
- 左栏是**纯文本** QListWidget：不创建任何缩略图/``QPixmap``，只用自绘
  delegate 画底色与边框；组数量很大时内存仍只随文本增长。
- 右栏复用 ``review_qt`` 的卡片样式与加载器，但**只保留当前组**的 pixmap：
  切组时 ``deleteLater`` 旧卡片并清空所有强引用。
- 工作台不直接操作服务：所有决策通过注入的 ``decide(group, action, keeper)``
  回调交给 ``ScanService``（confirm/skip/suspend 语义与逐组模式一致）。
- X / Esc = 未处理组全部挂起（零移动），关闭时释放全部图像。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from config import (
    FONT_FAMILY,
    PRESS_ANIM_MS,
    PRESS_SHRINK_PX,
    REVIEW_CARD_BG,
    REVIEW_CARD_BORDER,
    REVIEW_CARD_BORDER_WIDTH,
    REVIEW_CARD_HEIGHT,
    REVIEW_CARD_RADIUS,
    REVIEW_CARD_SELECTED_BG,
    REVIEW_CARD_SELECTED_BORDER,
    REVIEW_CARD_SELECTED_BORDER_WIDTH,
    REVIEW_CARD_WIDTH,
    REVIEW_SHADOW_ALPHA,
    REVIEW_SHADOW_BLUR,
    REVIEW_SHADOW_COLOR,
    REVIEW_SHADOW_OFFSET_X,
    REVIEW_SHADOW_OFFSET_Y,
    THUMB_MAX,
    WORKBENCH_BUTTON_GAP,
    WORKBENCH_BUTTON_MIN_WIDTH,
    WORKBENCH_DISCARD_CONFIRM_NO,
    WORKBENCH_DISCARD_CONFIRM_TEXT,
    WORKBENCH_DISCARD_CONFIRM_TITLE,
    WORKBENCH_DISCARD_CONFIRM_YES,
    WORKBENCH_DISCARD_TEXT,
    WORKBENCH_CARD_AREA_PAD,
    WORKBENCH_CARD_FOCUS_BORDER,
    WORKBENCH_CARD_GAP,
    WORKBENCH_CARD_ITEM_GAP,
    WORKBENCH_CARD_PAD,
    WORKBENCH_EMPTY_BG,
    WORKBENCH_EMPTY_FG,
    WORKBENCH_EMPTY_MUTED,
    WORKBENCH_EMPTY_TITLE_FONT_SIZE,
    WORKBENCH_FOOTER_HINT,
    WORKBENCH_HANDLE_COLOR,
    WORKBENCH_HANDLE_WIDTH,
    WORKBENCH_HEADER_FONT_SIZE,
    WORKBENCH_HEIGHT,
    WORKBENCH_LEFT_BG,
    WORKBENCH_LEFT_MIN_WIDTH,
    WORKBENCH_LEFT_RATIO,
    WORKBENCH_LIST_BG,
    WORKBENCH_LIST_BORDER,
    WORKBENCH_LIST_DONE_FG,
    WORKBENCH_LIST_HOVER_BG,
    WORKBENCH_LIST_ITEM_HEIGHT,
    WORKBENCH_LIST_ITEM_MARGIN,
    WORKBENCH_LIST_ITEM_PAD_Y,
    WORKBENCH_LIST_PAD_X,
    WORKBENCH_LIST_PENDING_FG,
    WORKBENCH_LIST_RADIUS,
    WORKBENCH_LIST_SECONDARY_FG,
    WORKBENCH_LIST_SELECTED_BAR_COLOR,
    WORKBENCH_LIST_SELECTED_BAR_INSET,
    WORKBENCH_LIST_SELECTED_BAR_WIDTH,
    WORKBENCH_LIST_SELECTED_BG,
    WORKBENCH_MAX_SCREEN_RATIO,
    WORKBENCH_MIN_HEIGHT,
    WORKBENCH_MIN_WIDTH,
    WORKBENCH_NAME_GAP,
    WORKBENCH_PANEL_GAP,
    WORKBENCH_PANEL_PAD,
    WORKBENCH_RIGHT_BG,
    WORKBENCH_RIGHT_MIN_WIDTH,
    WORKBENCH_SPLITTER_LEFT,
    WORKBENCH_UNDO_TEXT,
    WORKBENCH_WIDTH,
)
from pixfilter_core import (
    TRASH_DIR_NAME,
    DuplicateGroup,
    ImageInfo,
    format_size,
    group_members,
    pick_default_keeper,
)
from review_qt import (
    PressBounceButton,
    QtImageViewer,
    _ClickableFrame,
    _ClickableLabel,
    _PREVIEW_ERRORS,
    _default_log,
    _ensure_qt_main_thread,
    _font,
    _load_pixmap,
    _styles,
)

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_SKIPPED = "skipped"
STATUS_DISCARDED = "discarded"
STATUS_SUSPENDED = "suspended"

DONE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
# 每个列表项的结构化行数据：第 0 行状态；其余行可带 name/size/tail 三段。
LINES_ROLE = int(Qt.ItemDataRole.UserRole) + 2


def _status_text(status: str) -> str:
    if status == STATUS_CONFIRMED:
        return "✅ 已处理"
    if status == STATUS_SKIPPED:
        return "已跳过"
    if status == STATUS_DISCARDED:
        return "已处理（全部丢弃）"
    if status == STATUS_SUSPENDED:
        return "已挂起"
    return "⏳待处理"


class _WorkbenchItemDelegate(QStyledItemDelegate):
    """纯文本三行绘制；不读取 icon、不创建任何 pixmap。

    选中态是「浅蓝圆角底 + 左侧 2px 主题蓝细条」，不再画四周粗边框；
    项间留白由 ``QListWidget.setSpacing`` 负责，绘制时不额外内缩。
    """

    selected_bg = WORKBENCH_LIST_SELECTED_BG
    hover_bg = WORKBENCH_LIST_HOVER_BG
    item_bg = WORKBENCH_LIST_BG
    radius = WORKBENCH_LIST_RADIUS
    bar_color = WORKBENCH_LIST_SELECTED_BAR_COLOR
    bar_width = WORKBENCH_LIST_SELECTED_BAR_WIDTH
    bar_inset = WORKBENCH_LIST_SELECTED_BAR_INSET
    selected_border_width = 0
    done_fg = WORKBENCH_LIST_DONE_FG
    pending_fg = WORKBENCH_LIST_PENDING_FG
    secondary_fg = WORKBENCH_LIST_SECONDARY_FG

    def __init__(self, scale: float = 1.0, parent=None) -> None:
        super().__init__(parent)
        self.scale = max(1.0, float(scale))
        # 测试/诊断用：最近一次真实绘制出的每行文本与区域（文本坐标，非控件坐标）。
        self.last_rows: dict[int, dict] = {}

    def _px(self, value: int) -> int:
        return max(1, int(round(value * self.scale)))

    def paint(self, painter: QPainter, option, index) -> None:  # noqa: N802
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        done = bool(index.data(DONE_ROLE))
        rect = QRectF(option.rect)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        background = (
            self.selected_bg if selected
            else self.hover_bg if hovered
            else self.item_bg
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(background))
        radius = float(self._px(self.radius))
        painter.drawRoundedRect(rect, radius, radius)

        if selected:
            inset = float(self._px(self.bar_inset))
            bar_width = float(self._px(self.bar_width))
            bar = QRectF(
                rect.left() + inset,
                rect.top() + inset,
                bar_width,
                max(1.0, rect.height() - inset * 2),
            )
            painter.setBrush(QColor(self.bar_color))
            painter.drawRoundedRect(bar, bar_width / 2, bar_width / 2)

        font = QFont(FONT_FAMILY, 10)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        lines = text.split("\n")
        structured = index.data(LINES_ROLE) or []
        block_height = metrics.height() * len(lines)
        pad_y = float(self._px(WORKBENCH_LIST_ITEM_PAD_Y))
        top = rect.top() + pad_y + max(0.0, (rect.height() - pad_y * 2 - block_height) / 2)
        left = rect.left() + self._px(WORKBENCH_LIST_PAD_X)
        right = rect.right() - self._px(WORKBENCH_LIST_PAD_X)
        available = max(1.0, right - left)
        gap = float(self._px(WORKBENCH_NAME_GAP))
        recorded: list[dict] = []
        for line_no, line in enumerate(lines):
            if done:
                color = WORKBENCH_LIST_DONE_FG
            elif line_no == 0:
                color = WORKBENCH_LIST_PENDING_FG
            else:
                color = WORKBENCH_LIST_SECONDARY_FG
            painter.setPen(QColor(color))
            y = top + line_no * metrics.height() + metrics.ascent()
            entry = structured[line_no] if line_no < len(structured) else {}
            size_text = str(entry.get("size", "") or "")
            if not size_text:
                drawn = metrics.elidedText(
                    line, Qt.TextElideMode.ElideRight, int(available)
                )
                painter.drawText(QPointF(left, y), drawn)
                recorded.append(
                    {
                        "kind": "text",
                        "text": drawn,
                        "x": [left, left + metrics.horizontalAdvance(drawn)],
                    }
                )
                continue
            # 先保文件大小：预留 size 的宽度与固定间距，剩余宽度才给文件名；
            # 「等 N 张」是附加信息，空间不足时优先保留文件名。
            name = str(entry.get("name", "") or "")
            tail = str(entry.get("tail", "") or "")
            size_width = metrics.horizontalAdvance(size_text)
            ellipsis = metrics.horizontalAdvance("…")
            name_budget = available - size_width - gap
            if name_budget < ellipsis:
                drawn_name = ""
            else:
                drawn_name = metrics.elidedText(
                    name, Qt.TextElideMode.ElideRight, int(name_budget)
                )
            if drawn_name == name and tail:
                if metrics.horizontalAdvance(name) + metrics.horizontalAdvance(tail) <= name_budget:
                    drawn_tail = tail
                else:
                    drawn_tail = ""
            else:
                drawn_tail = ""
            name_width = metrics.horizontalAdvance(drawn_name)
            tail_width = metrics.horizontalAdvance(drawn_tail)
            size_x = right - size_width - tail_width
            if drawn_name:
                painter.drawText(QPointF(left, y), drawn_name)
            painter.drawText(QPointF(size_x, y), size_text)
            if drawn_tail:
                painter.drawText(QPointF(size_x + size_width, y), drawn_tail)
            recorded.append(
                {
                    "kind": "name_size",
                    "name": drawn_name,
                    "name_x": [left, left + name_width],
                    "size": size_text,
                    "size_x": [size_x, size_x + size_width],
                    "tail": drawn_tail,
                    "gap": size_x - (left + name_width),
                }
            )
        painter.restore()
        self.last_rows[index.row()] = {
            "rect": [rect.left(), rect.top(), rect.width(), rect.height()],
            "available": available,
            "left": left,
            "right": right,
            "lines": recorded,
        }

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(0, self._px(WORKBENCH_LIST_ITEM_HEIGHT))


class _ReviewCard(_ClickableFrame):
    """工作台卡片：点击选中 + 按压回弹，控件 rect 永不变化。

    按压动画只改变 ``pressProgress``，``paintEvent`` 以卡片中心为锚点
    ``translate/scale`` 绘制底色与边框；不动 geometry，父 ``QHBoxLayout``
    因此不会收到 ``LayoutRequest``，相邻卡片也不会位移。
    """

    def _get_press_progress(self) -> float:
        return getattr(self, "_press_progress", 0.0)

    def _set_press_progress(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        if abs(value - self._press_progress) < 1e-4:
            return
        self._press_progress = value
        self.update()

    pressProgress = Property(float, _get_press_progress, _set_press_progress)

    doubleClicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._press_progress = 0.0
        self._paint_bg = REVIEW_CARD_BG
        self._paint_border = REVIEW_CARD_BORDER
        self._paint_border_width = REVIEW_CARD_BORDER_WIDTH
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._press_anim = QPropertyAnimation(self, b"pressProgress", self)
        self._press_anim.setDuration(PRESS_ANIM_MS)
        self._press_anim.setEasingCurve(QEasingCurve.Type.OutQuad)

    # ---------- 动画状态（测试可直接读取） ----------
    def press_progress(self) -> float:
        return self._press_progress

    def press_animation(self) -> QPropertyAnimation:
        return self._press_anim

    def is_press_animating(self) -> bool:
        return self._press_anim.state() == QAbstractAnimation.State.Running

    def _animate_press(self, target: float) -> None:
        # 复用同一个动画对象：连击只重启，不新建，动画数量恒定。
        self._press_anim.stop()
        self._press_anim.setStartValue(self._press_progress)
        self._press_anim.setEndValue(float(target))
        self._press_anim.setDuration(PRESS_ANIM_MS)
        self._press_anim.start()

    def _snap_release(self) -> None:
        self._press_anim.stop()
        self._press_progress = 0.0
        self.update()

    def set_selection_colors(self, selected: bool) -> None:
        """记录当前选中态底色/边框，供按压时手动重绘（仍取自 config）。"""
        if selected:
            self._paint_bg = REVIEW_CARD_SELECTED_BG
            self._paint_border = REVIEW_CARD_SELECTED_BORDER
            self._paint_border_width = REVIEW_CARD_SELECTED_BORDER_WIDTH
        else:
            self._paint_bg = REVIEW_CARD_BG
            self._paint_border = REVIEW_CARD_BORDER
            self._paint_border_width = REVIEW_CARD_BORDER_WIDTH

    def mousePressEvent(self, event) -> None:  # noqa: N802
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self._animate_press(1.0)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        self._animate_press(0.0)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._press_progress <= 0.001:
            super().paintEvent(event)
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = max(1, self.width())
        height = max(1, self.height())
        shrink = PRESS_SHRINK_PX * self._press_progress
        scale_x = max(0.0, 1.0 - shrink / width)
        scale_y = max(0.0, 1.0 - shrink / height)
        painter.translate(width / 2.0, height / 2.0)
        painter.scale(scale_x, scale_y)
        painter.translate(-width / 2.0, -height / 2.0)
        border = float(self._paint_border_width)
        rect = QRectF(
            border / 2.0,
            border / 2.0,
            max(1.0, width - border),
            max(1.0, height - border),
        )
        painter.setPen(QPen(QColor(self._paint_border), border))
        painter.setBrush(QColor(self._paint_bg))
        painter.drawRoundedRect(rect, REVIEW_CARD_RADIUS, REVIEW_CARD_RADIUS)
        painter.end()


class QtReviewWorkbench(QDialog):
    """一次打开、任意跳转的批处理复核工作台。"""

    closed = Signal()

    def __init__(
        self,
        groups: list,
        root: Path,
        *,
        log: Optional[Callable[[str, str], None]] = None,
        decide: Optional[Callable[[object, str, object], object]] = None,
        undo_group: Optional[Callable[[object], object]] = None,
        parent: Optional[QWidget] = None,
        initial_index: int = 0,
    ) -> None:
        super().__init__(parent)
        _ensure_qt_main_thread("QtReviewWorkbench")
        if not groups:
            raise ValueError("工作台至少需要一组待确认图片")
        self.groups: list[DuplicateGroup] = list(groups)
        self.root = Path(root)
        self._log = log or _default_log
        self._decide = decide
        self._undo_group = undo_group
        self._styles = _styles()
        self.ui_scale = self._compute_scale()

        self._status = [STATUS_PENDING] * len(self.groups)
        # 每个组自己的移动批次是否仍在撤销栈中；切组时按该组状态决定按钮亮/灰。
        self._group_undoable = [False] * len(self.groups)
        # “最近处理过的组”：空态或当前组不可撤时，撤销按钮回退到这里的批次。
        # 用 id(group) 作为稳定 key，另存处理顺序用于找“最近仍有批次”的组。
        self._last_processed_key: Optional[object] = None
        self._processed_keys: list[object] = []
        self._undo_busy = False
        self._summary = {
            "confirmed": 0,
            "moved": 0,
            "skipped": 0,
            "discarded": 0,
            "suspended": 0,
        }
        self._complete = False
        self._discarded = False
        self._closed_decisions = False
        self._sized_once = False
        self._current_index = -1
        self.members: list[ImageInfo] = []
        self.default: Optional[ImageInfo] = None

        self._pixmaps: list = []
        self._thumbnail_labels: list[QLabel] = []
        self._placeholders: list[QLabel] = []
        self._cards: dict[str, _ClickableFrame] = {}
        self._card_effects: list[QGraphicsDropShadowEffect] = []
        # 当前组要保留的路径（默认由 pick_default_keeper 决定，点击卡片可改）。
        self._selected = ""
        self._viewers: list[QtImageViewer] = []

        self.setWindowTitle("PixFilter · 批量复核工作台")
        self.setStyleSheet(self._styles["dialog"])
        self.setMinimumSize(WORKBENCH_MIN_WIDTH, WORKBENCH_MIN_HEIGHT)
        self.resize(self._preferred_size())

        self._build_ui()
        start = max(0, min(int(initial_index), len(self.groups) - 1))
        self._select_group(start)

    # ---------- 当前组 / 单例复用 ----------
    @property
    def current_index(self) -> int:
        """右栏当前渲染的组下标（-1 表示已全部处理完毕）。"""
        return self._current_index

    def focus_group(self, index: int) -> None:
        """把已打开的工作台切到指定组并置前；不创建第二个窗口。"""
        if not 0 <= index < len(self.groups):
            return
        self._select_group(index)
        self.raise_()
        self.activateWindow()

    # ---------- 尺寸 ----------
    @staticmethod
    def _compute_scale() -> float:
        screen = QApplication.primaryScreen()
        dpi = screen.logicalDotsPerInch() if screen is not None else 96.0
        return min(1.5, max(1.0, float(dpi) / 96.0))

    def _px(self, value: int) -> int:
        """把 config 中的像素常量按 DPI 缩放（最小 1px）。"""
        return max(1, int(round(value * self.ui_scale)))

    def _preferred_size(self) -> QSize:
        screen = QApplication.primaryScreen()
        geometry = screen.availableGeometry() if screen is not None else None
        width = int(WORKBENCH_WIDTH * self.ui_scale)
        height = int(WORKBENCH_HEIGHT * self.ui_scale)
        if geometry is not None:
            width = min(width, int(geometry.width() * WORKBENCH_MAX_SCREEN_RATIO))
            height = min(height, int(geometry.height() * WORKBENCH_MAX_SCREEN_RATIO))
        return QSize(
            max(WORKBENCH_MIN_WIDTH, width),
            max(WORKBENCH_MIN_HEIGHT, height),
        )

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        if not self._sized_once:
            self._sized_once = True
            total = max(self.splitter.width(), WORKBENCH_MIN_WIDTH)
            left = max(int(total * WORKBENCH_LEFT_RATIO), WORKBENCH_SPLITTER_LEFT)
            self.splitter.setSizes([left, max(total - left, 1)])

    def _list_qss(self) -> str:
        return (
            f"QListWidget {{ background-color: {WORKBENCH_LIST_BG};"
            f" border: 1px solid {WORKBENCH_LIST_BORDER};"
            f" border-radius: {self._px(WORKBENCH_LIST_RADIUS)}px;"
            " outline: 0; }"
        )

    # ---------- 布局 ----------
    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(self._px(WORKBENCH_HANDLE_WIDTH))
        self.splitter.setStyleSheet(
            "QSplitter::handle {"
            f" background-color: {WORKBENCH_HANDLE_COLOR};"
            " border: none; }"
        )
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.addWidget(self._build_left())
        self.splitter.addWidget(self._build_right())
        outer.addWidget(self.splitter, 1)

    def _build_left(self) -> QWidget:
        left = QWidget()
        left.setObjectName("workbenchLeft")
        left.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        left.setStyleSheet(
            f"QWidget#workbenchLeft {{ background-color: {WORKBENCH_LEFT_BG}; }}"
        )
        left.setMinimumWidth(WORKBENCH_LEFT_MIN_WIDTH)
        pad = self._px(WORKBENCH_PANEL_PAD)
        layout = QVBoxLayout(left)
        layout.setContentsMargins(pad, pad, pad, pad)
        layout.setSpacing(self._px(WORKBENCH_PANEL_GAP))
        heading = QLabel(f"待处理分组 · 共 {len(self.groups)} 组")
        heading.setFont(_font(WORKBENCH_HEADER_FONT_SIZE, bold=True))
        heading.setStyleSheet(self._styles["title"])
        layout.addWidget(heading)

        self.list_widget = QListWidget()
        self.list_widget.setObjectName("workbenchList")
        self.list_widget.setWordWrap(False)
        self.list_widget.setUniformItemSizes(True)
        self.list_widget.setMouseTracking(True)
        self.list_widget.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.list_widget.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.list_widget.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        # QListView 会在项的上下各加一份 spacing，因此取一半才能得到 4px 净间隙。
        self.list_widget.setSpacing(
            max(1, (self._px(WORKBENCH_LIST_ITEM_MARGIN) + 1) // 2)
        )
        self.list_widget.setStyleSheet(self._list_qss())
        self.list_delegate = _WorkbenchItemDelegate(self.ui_scale)
        self.list_widget.setItemDelegate(self.list_delegate)
        self._list_items: list[QListWidgetItem] = []
        for index in range(len(self.groups)):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setSizeHint(QSize(0, self._px(WORKBENCH_LIST_ITEM_HEIGHT)))
            self._set_item_text(item, index)
            self.list_widget.addItem(item)
            self._list_items.append(item)
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        layout.addWidget(self.list_widget, 1)
        return left

    def _build_right(self) -> QWidget:
        right = QWidget()
        right.setObjectName("workbenchRight")
        right.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        right.setStyleSheet(
            f"QWidget#workbenchRight {{ background-color: {WORKBENCH_RIGHT_BG}; }}"
        )
        right.setMinimumWidth(WORKBENCH_RIGHT_MIN_WIDTH)
        pad = self._px(WORKBENCH_PANEL_PAD)
        layout = QVBoxLayout(right)
        layout.setContentsMargins(pad, pad, pad, pad)
        layout.setSpacing(self._px(WORKBENCH_PANEL_GAP))

        header = QHBoxLayout()
        self.right_title = QLabel("第 1/1 组")
        self.right_title.setFont(_font(WORKBENCH_HEADER_FONT_SIZE, bold=True))
        self.right_title.setStyleSheet(self._styles["title"])
        header.addWidget(self.right_title)
        header.addStretch(1)
        self.right_detail = QLabel()
        self.right_detail.setStyleSheet(self._styles["muted"])
        header.addWidget(self.right_detail)
        layout.addLayout(header)

        hint = QLabel(
            f"点击卡片选中要保留的一张，其余会在确认后移入“{TRASH_DIR_NAME}”。"
            "点击缩略图可查看大图；也可用 Tab 聚焦卡片后按空格/回车选中。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(self._styles["muted"])
        layout.addWidget(hint)

        self.stack = QStackedWidget()
        area_pad = self._px(WORKBENCH_CARD_AREA_PAD)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet(
            f"QScrollArea {{ border: 0; background-color: {WORKBENCH_RIGHT_BG}; }}"
        )
        self.scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.container = QWidget()
        self.container.setObjectName("workbenchContainer")
        self.container.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        self.container.setStyleSheet(
            "QWidget#workbenchContainer {"
            f" background-color: {WORKBENCH_RIGHT_BG}; }}"
        )
        self.container_layout = QVBoxLayout(self.container)
        self.container_layout.setContentsMargins(
            area_pad, area_pad, area_pad, area_pad
        )
        self.container_layout.setSpacing(0)
        self.container_layout.addStretch(1)
        self.card_row = QHBoxLayout()
        self.card_row.setContentsMargins(0, 0, 0, 0)
        self.card_row.setSpacing(self._px(WORKBENCH_CARD_GAP))
        self.container_layout.addLayout(self.card_row)
        self.container_layout.addStretch(1)
        self.scroll.setWidget(self.container)
        self.stack.addWidget(self.scroll)
        self.empty_page = self._build_empty()
        self.stack.addWidget(self.empty_page)
        layout.addWidget(self.stack, 1)

        # 提示文案放在按钮区上方、独占一行：只允许它自己换行/增高，
        # 绝不与按钮共享同一横向布局，从根上杜绝文字挤压按钮。
        footer = QVBoxLayout()
        footer.setSpacing(self._px(WORKBENCH_PANEL_GAP))
        footer_hint = QLabel(WORKBENCH_FOOTER_HINT)
        footer_hint.setWordWrap(True)
        # 提示文字可压缩：优先换行，绝不去挤压按钮；同时保留 heightForWidth，
        # 让换行后的高度被布局计算，而不是按单行高度裁切。
        hint_policy = QSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Minimum
        )
        hint_policy.setHeightForWidth(True)
        footer_hint.setSizePolicy(hint_policy)
        footer_hint.setStyleSheet(self._styles["muted"])
        self.footer_hint = footer_hint
        footer.addWidget(footer_hint)

        button_min = self._px(WORKBENCH_BUTTON_MIN_WIDTH)
        button_row = QHBoxLayout()
        button_row.setSpacing(self._px(WORKBENCH_BUTTON_GAP))
        self.btn_skip = PressBounceButton("跳过")
        self.btn_skip.setMinimumWidth(button_min)
        self.btn_skip.setStyleSheet(self._styles["secondary"])
        self.btn_skip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_skip.clicked.connect(self._skip)
        button_row.addWidget(self.btn_skip)

        self.btn_undo_group = PressBounceButton(WORKBENCH_UNDO_TEXT)
        self.btn_undo_group.setMinimumWidth(button_min)
        self.btn_undo_group.setStyleSheet(self._styles["secondary"])
        self.btn_undo_group.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_undo_group.setEnabled(False)
        self.btn_undo_group.clicked.connect(self._undo_current)
        button_row.addWidget(self.btn_undo_group)

        self.btn_discard = PressBounceButton(WORKBENCH_DISCARD_TEXT)
        self.btn_discard.setMinimumWidth(button_min)
        self.btn_discard.setStyleSheet(self._styles["discard"])
        self.btn_discard.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_discard.clicked.connect(self._discard_all)
        button_row.addWidget(self.btn_discard, 1)

        self.btn_confirm = PressBounceButton("保留选中的，移动其他图片")
        self.btn_confirm.setMinimumWidth(button_min)
        self.btn_confirm.setStyleSheet(self._styles["primary"])
        self.btn_confirm.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_confirm.clicked.connect(self._confirm)
        button_row.addWidget(self.btn_confirm, 1)
        footer.addLayout(button_row)
        self.footer_layout = footer
        self.button_row = button_row
        layout.addLayout(footer)
        return right

    def _build_empty(self) -> QWidget:
        empty = QWidget()
        empty.setStyleSheet(f"background-color: {WORKBENCH_EMPTY_BG};")
        layout = QVBoxLayout(empty)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title = QLabel("全部处理完毕")
        self.empty_title.setFont(_font(WORKBENCH_EMPTY_TITLE_FONT_SIZE, bold=True))
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_title.setStyleSheet(f"color: {WORKBENCH_EMPTY_FG};")
        layout.addWidget(self.empty_title)
        self.empty_summary = QLabel()
        self.empty_summary.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_summary.setWordWrap(True)
        self.empty_summary.setStyleSheet(f"color: {WORKBENCH_EMPTY_MUTED};")
        layout.addWidget(self.empty_summary)
        return empty

    # ---------- 左侧纯文本 ----------
    def _set_item_text(self, item: QListWidgetItem, index: int) -> None:
        group = self.groups[index]
        status = self._status[index]
        total = len(self.groups)
        members = group_members(group)
        first_line = f"第 {index + 1}/{total} 组 {_status_text(status)}"
        lines = [first_line]
        structured = [{"text": first_line}]
        if members:
            name = members[0].path.name
            size = format_size(members[0].file_size)
            lines.append(f"{name}  ·  {size}")
            structured.append({"name": name, "size": size})
        if len(members) > 1:
            name = members[1].path.name
            size = format_size(members[1].file_size)
            tail = f"  等 {len(members)} 张" if len(members) > 2 else ""
            lines.append(f"{name}  ·  {size}{tail}")
            structured.append({"name": name, "size": size, "tail": tail})
        item.setText("\n".join(lines))
        item.setData(LINES_ROLE, structured)
        item.setData(DONE_ROLE, status != STATUS_PENDING)
        item.setData(Qt.ItemDataRole.UserRole, index)

    def _on_row_changed(self, row: int) -> None:
        if 0 <= row < len(self.groups):
            self._render_group(row)

    def _select_group(self, index: int) -> None:
        if not 0 <= index < len(self.groups):
            return
        if self.list_widget.currentRow() == index:
            if self._current_index != index:
                self._render_group(index)
            return
        self.list_widget.setCurrentRow(index)
        # 某些情况下 currentRowChanged 不会同步触发，兜底渲染一次。
        if self._current_index != index:
            self._render_group(index)

    # ---------- 右侧渲染 ----------
    def _clear_cards(self) -> None:
        for label in self._thumbnail_labels:
            label.clear()
        self._thumbnail_labels.clear()
        self._pixmaps.clear()
        self._placeholders.clear()
        self._cards.clear()
        self._card_effects.clear()
        self._selected = ""
        while self.card_row.count():
            item = self.card_row.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()

    def _render_group(self, index: int, *, read_only: bool = False) -> None:
        self._clear_cards()
        self._current_index = index
        group = self.groups[index]
        self.members = group_members(group)
        self.default = pick_default_keeper(group)
        self._selected = str(self.default.path)
        configured = read_only or self._status[index] != STATUS_PENDING or self._complete
        kind_text = "完全重复" if group.kind == "exact" else "视觉相似"
        self.right_title.setText(
            f"第 {index + 1}/{len(self.groups)} 组 · {kind_text} · "
            f"{len(self.members)} 张"
        )
        self.right_detail.setText(
            f"最大 pHash 差异 {group.max_distance}"
            if group.kind == "similar"
            else ""
        )

        self.card_row.addStretch(1)
        for info in self.members:
            self.card_row.addWidget(self._build_card(info, configured))
        self.card_row.addStretch(1)
        self._refresh_status()
        self._update_buttons(configured)
        self.stack.setCurrentWidget(self.scroll)

    def _build_card(self, info: ImageInfo, read_only: bool) -> QFrame:
        card = _ReviewCard()
        card.setObjectName("reviewCard")
        card.setProperty("selected", False)
        card.setStyleSheet(self._card_sheet(False))
        card.setFixedSize(REVIEW_CARD_WIDTH, REVIEW_CARD_HEIGHT)
        card.setCursor(Qt.CursorShape.PointingHandCursor)
        card.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._cards[str(info.path)] = card

        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(REVIEW_SHADOW_BLUR)
        shadow.setOffset(REVIEW_SHADOW_OFFSET_X, REVIEW_SHADOW_OFFSET_Y)
        shadow_color = QColor(REVIEW_SHADOW_COLOR)
        shadow_color.setAlpha(REVIEW_SHADOW_ALPHA)
        shadow.setColor(shadow_color)
        card.setGraphicsEffect(shadow)
        self._card_effects.append(shadow)

        layout = QVBoxLayout(card)
        pad = self._px(WORKBENCH_CARD_PAD)
        layout.setContentsMargins(pad, pad, pad, pad)
        layout.setSpacing(self._px(WORKBENCH_CARD_ITEM_GAP))

        thumb = _ClickableLabel()
        thumb.setObjectName("cardThumb")
        thumb.setFixedSize(THUMB_MAX, THUMB_MAX)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        thumb.setStyleSheet(self._styles["thumb"])
        thumb.setCursor(Qt.CursorShape.PointingHandCursor)
        try:
            pixmap, _width, _height = _load_pixmap(
                info.path, THUMB_MAX, THUMB_MAX
            )
        except _PREVIEW_ERRORS as exc:
            display_path = str(info.path) if info.path is not None else ""
            self._log(
                f"[缩略图] 无法加载图片：{display_path}"
                f"（{type(exc).__name__}: {exc}）",
                "warn",
            )
            thumb.setText(
                "无法加载图片\n"
                f"{Path(display_path).name if display_path else '（空路径）'}\n"
                f"{type(exc).__name__}"
            )
            thumb.setWordWrap(True)
            thumb.setStyleSheet(self._styles["placeholder"])
            self._placeholders.append(thumb)
        else:
            thumb.setPixmap(pixmap)
            self._pixmaps.append(pixmap)
            self._thumbnail_labels.append(thumb)
        thumb.clicked.connect(lambda item=info: self._select_card(item))
        layout.addWidget(thumb, 0, Qt.AlignmentFlag.AlignHCenter)

        # 精简后卡片只剩三行：缩略图、文件名（超宽省略）、文件大小。
        name = QLabel()
        name.setObjectName("cardName")
        name.setFont(_font(10, bold=True))
        name.setStyleSheet(self._styles["title"])
        name.setWordWrap(False)
        metrics = QFontMetrics(name.font())
        available = max(1, REVIEW_CARD_WIDTH - 2 * pad)
        name.setText(
            metrics.elidedText(
                info.path.name, Qt.TextElideMode.ElideMiddle, available
            )
        )
        name.setToolTip(info.path.name)
        layout.addWidget(name)

        size = QLabel(format_size(info.file_size))
        size.setObjectName("cardSize")
        size.setStyleSheet(self._styles["muted"])
        size.setWordWrap(False)
        layout.addWidget(size)
        layout.addStretch(1)

        if not read_only:
            card.clicked.connect(lambda item=info: self._select_card(item))
        # 双击卡片（含缩略图区域）打开大图；单击只切换选中。
        card.doubleClicked.connect(lambda item=info: self._open_viewer(item))
        return card

    def _card_sheet(self, selected: bool) -> str:
        """卡片 QSS；追加焦点描边规则，颜色来自 config。"""
        base = self._styles["card_selected" if selected else "card"]
        return base + (
            f"QFrame#reviewCard:focus {{ border: "
            f"{REVIEW_CARD_SELECTED_BORDER_WIDTH}px solid "
            f"{WORKBENCH_CARD_FOCUS_BORDER}; }}"
        )

    def _selected_path(self) -> str:
        if self._selected and self._selected in self._cards:
            return self._selected
        return str(self.default.path) if self.default is not None else ""

    def _current_keeper(self) -> Optional[ImageInfo]:
        """confirm 使用的 keeper：优先当前选中的卡片，否则退回默认规则。"""
        selected = self._selected_path()
        for info in self.members:
            if str(info.path) == selected:
                return info
        if 0 <= self._current_index < len(self.groups):
            return pick_default_keeper(self.groups[self._current_index])
        return None

    def _select_card(self, info: ImageInfo) -> None:
        if not 0 <= self._current_index < len(self.groups):
            return
        if self._status[self._current_index] != STATUS_PENDING or self._complete:
            return
        path = str(info.path)
        if path == self._selected:
            return
        self._selected = path
        self._refresh_status()

    def _refresh_status(self) -> None:
        selected = self._selected_path()
        for path, card in self._cards.items():
            is_selected = path == selected
            card.setProperty("selected", is_selected)
            card.setStyleSheet(self._card_sheet(is_selected))
            card.set_selection_colors(is_selected)

    def _index_for_key(self, key: object) -> int:
        for index, group in enumerate(self.groups):
            if id(group) == key:
                return index
        return -1

    def _undo_target_index(self) -> int:
        """撤销按钮的目标组。

        优先当前仍在查看、且批次仍可撤的已处理组；若当前是待处理组或空态，
        回退到“最近处理过且仍有批次”的组，解决最后一组处理完后按钮永远灰的问题。
        """
        index = self._current_index
        if (
            0 <= index < len(self.groups)
            and self._status[index] in (STATUS_CONFIRMED, STATUS_DISCARDED)
            and self._group_undoable[index]
        ):
            return index
        if self._last_processed_key is not None:
            index = self._index_for_key(self._last_processed_key)
            if (
                index >= 0
                and self._group_undoable[index]
                and self._status[index] in (STATUS_CONFIRMED, STATUS_DISCARDED)
            ):
                return index
        for key in reversed(self._processed_keys):
            index = self._index_for_key(key)
            if (
                index >= 0
                and self._group_undoable[index]
                and self._status[index] in (STATUS_CONFIRMED, STATUS_DISCARDED)
            ):
                return index
        return -1

    def _update_buttons(self, read_only: bool) -> None:
        allowed = (
            not read_only
            and not self._complete
            and 0 <= self._current_index < len(self.groups)
            and self._status[self._current_index] == STATUS_PENDING
        )
        self.btn_skip.setEnabled(allowed)
        self.btn_discard.setEnabled(allowed)
        self.btn_confirm.setEnabled(allowed)

        # 空态下 _current_index 为 -1，但最近处理组的批次仍在栈中时按钮必须点亮。
        undo_allowed = (
            not self._undo_busy
            and self._undo_group is not None
            and self._undo_target_index() >= 0
        )
        self.btn_undo_group.setEnabled(undo_allowed)

    def _open_viewer(self, info: ImageInfo) -> None:
        try:
            viewer = QtImageViewer(info, log=self._log, parent=self)
        except Exception as exc:
            self._log(
                f"[大图预览] 打开失败：{info.path}"
                f"（{type(exc).__name__}: {exc}）",
                "warn",
            )
            return
        self._viewers.append(viewer)
        viewer.show()

    # ---------- 决策 ----------
    def _confirm(self) -> None:
        self._decide_current("confirm", self._current_keeper())

    def _skip(self) -> None:
        self._decide_current("skip")

    def _discard_confirm_text(self, group: DuplicateGroup) -> str:
        return WORKBENCH_DISCARD_CONFIRM_TEXT.format(count=len(group_members(group)))

    def _ask_discard(self, group: DuplicateGroup) -> bool:
        """二次确认整组丢弃；默认按钮为“取消”，降低误触风险。"""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(WORKBENCH_DISCARD_CONFIRM_TITLE)
        box.setText(self._discard_confirm_text(group))
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel
        )
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.setEscapeButton(QMessageBox.StandardButton.Cancel)
        yes_button = box.button(QMessageBox.StandardButton.Yes)
        if yes_button is not None:
            yes_button.setText(WORKBENCH_DISCARD_CONFIRM_YES)
        no_button = box.button(QMessageBox.StandardButton.Cancel)
        if no_button is not None:
            no_button.setText(WORKBENCH_DISCARD_CONFIRM_NO)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _discard_all(self) -> None:
        if self._complete:
            return
        index = self._current_index
        if not 0 <= index < len(self.groups):
            return
        if self._status[index] != STATUS_PENDING:
            return
        if not self._ask_discard(self.groups[index]):
            return
        self._decide_current("discard_all", None)

    def _undo_current(self) -> None:
        """只撤销最近可撤组的移动批次，不影响其他组或全局撤销栈顺序。"""
        if self._undo_busy or self._undo_group is None:
            return
        index = self._undo_target_index()
        if index < 0:
            return

        group = self.groups[index]
        previous = self._status[index]
        self._undo_busy = True
        self.btn_undo_group.setEnabled(False)
        try:
            result = self._undo_group(group)
        except Exception as exc:
            self._log(
                f"[撤销本组] 调用失败：{type(exc).__name__}: {exc}", "error"
            )
            result = None
        finally:
            self._undo_busy = False

        if result is None or not getattr(result, "ok", False):
            reason = getattr(result, "error", "") if result is not None else ""
            if reason == "missing":
                # 批次已不在栈中（例如已被全局撤销）：按钮回灰，避免反复误点。
                self._group_undoable[index] = False
            self._log(
                "[撤销本组] 未能撤销"
                + (f"：{reason}" if reason else "：当前有撤销任务正在进行"),
                "warn",
            )
            self._update_buttons(False)
            return

        restored = int(getattr(result, "restored", 0))
        skipped = int(getattr(result, "skipped", 0))
        cleanup = getattr(result, "cleanup", "")
        if previous == STATUS_CONFIRMED:
            self._summary["confirmed"] = max(0, self._summary["confirmed"] - 1)
        else:
            self._summary["discarded"] = max(0, self._summary["discarded"] - 1)
        self._summary["moved"] = max(0, self._summary["moved"] - restored)
        self._status[index] = STATUS_PENDING
        self._group_undoable[index] = False
        self._complete = False
        # 该组批次已撤：从“最近处理”记录中移除，后续按剩余批次回退。
        key = id(group)
        self._processed_keys = [item for item in self._processed_keys if item != key]
        if self._last_processed_key == key:
            self._last_processed_key = (
                self._processed_keys[-1] if self._processed_keys else None
            )
        self._set_item_text(self._list_items[index], index)
        self._refresh_item_state(index)
        # 状态回待处理：重新渲染卡片，keeper 恢复默认可选、按钮恢复可决策。
        # 若当前处于“全部处理完毕”空态，这一步也会把右侧切回该组对比界面。
        if self.list_widget.currentRow() == index:
            self._render_group(index)
        else:
            self.list_widget.setCurrentRow(index)
        suffix = "，回收站已清理" if cleanup == "removed" else ""
        self._log(
            f"[撤销本组] 第 {index + 1} 组已还原 {restored} 张，"
            f"跳过 {skipped} 张{suffix}",
            "info",
        )

    def _mark_processed(self, index: int) -> None:
        """记录“最近处理过的组”，供空态/待处理视角下的撤销按钮回退。"""
        key = id(self.groups[index])
        self._last_processed_key = key
        self._processed_keys.append(key)

    def _decide_current(self, action: str, keeper=None) -> None:
        if self._complete:
            return
        index = self._current_index
        if not 0 <= index < len(self.groups):
            return
        if self._status[index] != STATUS_PENDING:
            return
        group = self.groups[index]
        # 整组丢弃没有 keeper，不能退回默认保留项。
        if keeper is None and action != "discard_all":
            keeper = pick_default_keeper(group)
        outcome = None
        if self._decide is not None:
            try:
                outcome = self._decide(group, action, keeper)
            except Exception as exc:
                self._log(
                    f"[工作台] 决策回调失败：{type(exc).__name__}: {exc}", "error"
                )
                return
        if action == "confirm":
            duplicate = bool(getattr(outcome, "duplicate", False))
            suspended = bool(getattr(outcome, "suspended", False))
            if not duplicate and not suspended:
                self._summary["confirmed"] += 1
                moves = getattr(outcome, "moves", []) or []
                moved_count = sum(
                    1 for result in moves if getattr(result, "ok", False)
                )
                self._summary["moved"] += moved_count
                self._status[index] = STATUS_CONFIRMED
                self._group_undoable[index] = moved_count > 0
                if moved_count > 0:
                    self._mark_processed(index)
            else:
                self._summary["suspended"] += 1
                self._status[index] = STATUS_SUSPENDED
        elif action == "discard_all":
            duplicate = bool(getattr(outcome, "duplicate", False))
            suspended = bool(getattr(outcome, "suspended", False))
            if not duplicate and not suspended:
                moves = getattr(outcome, "moves", []) or []
                moved_count = sum(
                    1 for result in moves if getattr(result, "ok", False)
                )
                self._summary["moved"] += moved_count
                self._summary["discarded"] += 1
                self._status[index] = STATUS_DISCARDED
                self._group_undoable[index] = moved_count > 0
                if moved_count > 0:
                    self._mark_processed(index)
            else:
                self._summary["suspended"] += 1
                self._status[index] = STATUS_SUSPENDED
        elif action == "skip":
            self._summary["skipped"] += 1
            self._status[index] = STATUS_SKIPPED
        else:
            self._summary["suspended"] += 1
            self._status[index] = STATUS_SUSPENDED
        self._set_item_text(self._list_items[index], index)
        self._refresh_item_state(index)
        self._advance()

    def _refresh_item_state(self, index: int) -> None:
        item = self._list_items[index]
        item.setData(DONE_ROLE, self._status[index] != STATUS_PENDING)
        self.list_widget.viewport().update()

    def _advance(self) -> None:
        total = len(self.groups)
        start = self._current_index
        for offset in range(1, total + 1):
            index = (start + offset) % total
            if self._status[index] == STATUS_PENDING:
                self._select_group(index)
                return
        self._show_complete()

    def _show_complete(self) -> None:
        self._complete = True
        self._clear_cards()
        self._current_index = -1
        self.right_title.setText("批量复核结果")
        self.right_detail.setText("")
        self.empty_summary.setText(self._summary_text())
        self.stack.setCurrentWidget(self.empty_page)
        # 空态下三个决策按钮禁用，但“撤销本组”仍按最近处理组的批次状态点亮。
        self._update_buttons(True)
        self._refresh_item_state(0)
        self.list_widget.viewport().update()

    def _summary_text(self) -> str:
        text = (
            f"确认 {self._summary['confirmed']} 组 · "
            f"移动 {self._summary['moved']} 张 · "
            f"跳过 {self._summary['skipped']} 组"
        )
        if self._summary["discarded"]:
            text += f" · 整组丢弃 {self._summary['discarded']} 组"
        if self._summary["suspended"]:
            text += f" · 挂起 {self._summary['suspended']} 组"
        return text

    def summary_snapshot(self) -> dict[str, int]:
        return dict(self._summary)

    # ---------- 关闭 ----------
    def _suspend_pending(self) -> None:
        if self._complete or self._discarded:
            return
        for index, status in enumerate(list(self._status)):
            if status != STATUS_PENDING:
                continue
            group = self.groups[index]
            if self._decide is not None:
                try:
                    self._decide(group, "suspend", pick_default_keeper(group))
                except Exception as exc:
                    self._log(
                        f"[工作台] 挂起回调失败：{type(exc).__name__}: {exc}",
                        "error",
                    )
            self._status[index] = STATUS_SUSPENDED
            self._summary["suspended"] += 1
            self._set_item_text(self._list_items[index], index)
            self._refresh_item_state(index)

    def _close_viewers(self) -> None:
        for viewer in self._viewers:
            viewer.close()
        self._viewers.clear()

    def discard(self) -> None:
        """由外部取消流程调用：服务已挂起未确认组，这里不再重复决策。"""
        self._discarded = True
        self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_X):
            self.close()
            return
        super().keyPressEvent(event)

    def reject(self) -> None:
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._closed_decisions:
            self._closed_decisions = True
            self._suspend_pending()
            self._close_viewers()
            self._clear_cards()
            self.closed.emit()
        event.accept()
