"""PixFilter 的 PySide6 对比弹窗（实验路径）。

本模块只在显式启用 Qt 后端时才被导入；Tk 默认路径不依赖 PySide6。
线程约束：
- 导入本模块不会创建 QApplication 或任何窗口；``ensure_qapplication`` 必须在主线程调用。
- ``QtReviewDialog`` / ``QtImageViewer`` 只能在 Qt 主线程创建，工作线程必须通过
  ``QtReviewBridge`` 的 queued signal 投递（服务回调可能来自工作线程）。
- 缩略图 ``QPixmap`` 由弹窗实例强引用，避免被 GC 回收后显示空白。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, Optional

from PIL import Image, UnidentifiedImageError

from PySide6.QtCore import (
    Property,
    QAbstractAnimation,
    QEasingCurve,
    QEvent,
    QObject,
    QPropertyAnimation,
    QSize,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from config import (
    ACCENT,
    ACCENT_HOVER,
    ACCENT_PRESS,
    AMBER,
    BG,
    DIALOG_BODY_PADX,
    DIALOG_BODY_PADY,
    DIALOG_CARD_PADX,
    DIALOG_CARD_PADY,
    DIALOG_CONTENT_PAD,
    FONT_FAMILY,
    GREEN,
    MUTED,
    PRESS_ANIM_MS,
    PRESS_SHRINK_PX,
    QT_SCROLLBAR_GROOVE,
    QT_SCROLLBAR_HANDLE,
    QT_SCROLLBAR_HANDLE_HOVER,
    QT_SCROLLBAR_HANDLE_PRESS,
    QT_SCROLLBAR_MARGIN,
    QT_SCROLLBAR_MIN_LENGTH,
    QT_SCROLLBAR_WIDTH,
    REVIEW_BUTTON_FONT_SIZE,
    REVIEW_BUTTON_PAD_X,
    REVIEW_BUTTON_PAD_Y,
    REVIEW_BUTTON_RADIUS,
    REVIEW_CARD_BG,
    REVIEW_CARD_BORDER,
    REVIEW_CARD_BORDER_WIDTH,
    REVIEW_CARD_HEIGHT,
    REVIEW_CARD_HOVER_BG,
    REVIEW_CARD_HOVER_BORDER,
    REVIEW_CARD_RADIUS,
    REVIEW_CARD_SELECTED_BG,
    REVIEW_CARD_SELECTED_BORDER,
    REVIEW_CARD_SELECTED_BORDER_WIDTH,
    REVIEW_CARD_SELECTED_HOVER_BORDER,
    REVIEW_CARD_SHADOW_PAD,
    REVIEW_CARD_SPACING,
    REVIEW_CARD_WIDTH,
    REVIEW_DISCARD_BG,
    REVIEW_DISCARD_BORDER,
    REVIEW_DISCARD_DISABLED_BG,
    REVIEW_DISCARD_DISABLED_FG,
    REVIEW_DISCARD_FG,
    REVIEW_DISCARD_HOVER_BG,
    REVIEW_DISCARD_PRESS_BG,
    REVIEW_EXTRA_WIDTH,
    REVIEW_FOOTER_WRAP,
    REVIEW_MAX_HEIGHT,
    REVIEW_MIN_HEIGHT,
    REVIEW_MIN_WIDTH,
    REVIEW_OPEN_MIN_WIDTH,
    REVIEW_PLACEHOLDER_BG,
    REVIEW_PLACEHOLDER_FG,
    REVIEW_PRIMARY_DISABLED_BG,
    REVIEW_PRIMARY_DISABLED_FG,
    REVIEW_PRIMARY_FG,
    REVIEW_RADIO_BG,
    REVIEW_RADIO_BORDER,
    REVIEW_RADIO_CHECKED_BG,
    REVIEW_RADIO_FOCUS_BG,
    REVIEW_RADIO_FONT_SIZE,
    REVIEW_RADIO_HOVER_BORDER,
    REVIEW_RADIO_INDICATOR_SIZE,
    REVIEW_RADIO_PAD,
    REVIEW_RADIO_RADIUS,
    REVIEW_SCREEN_HEIGHT_RATIO,
    REVIEW_SCREEN_WIDTH_RATIO,
    REVIEW_SECONDARY_BG,
    REVIEW_SECONDARY_BORDER,
    REVIEW_SECONDARY_DISABLED_BG,
    REVIEW_SECONDARY_DISABLED_FG,
    REVIEW_SECONDARY_FG,
    REVIEW_SECONDARY_HOVER_BG,
    REVIEW_SECONDARY_HOVER_BORDER,
    REVIEW_SECONDARY_PRESS_BG,
    REVIEW_SECONDARY_PRESS_BORDER,
    REVIEW_SHADOW_ALPHA,
    REVIEW_SHADOW_BLUR,
    REVIEW_SHADOW_COLOR,
    REVIEW_SHADOW_OFFSET_X,
    REVIEW_SHADOW_OFFSET_Y,
    REVIEW_THUMB_RADIUS,
    TEXT,
    THUMB_BG,
    THUMB_MAX,
    VIEWER_BG,
    VIEWER_MIN_HEIGHT,
    VIEWER_MIN_WIDTH,
    VIEWER_MUTED,
    VIEWER_SCREEN_RATIO,
    VIEWER_TEXT,
)
from pixfilter_core import (
    TRASH_DIR_NAME,
    DuplicateGroup,
    ImageInfo,
    format_size,
    group_members,
    pick_default_keeper,
)

_PREVIEW_ERRORS = (
    OSError,
    UnidentifiedImageError,
    getattr(Image, "DecompressionBombError", OSError),
    MemoryError,
    Exception,
)


def _default_log(message: str, kind: str = "info") -> None:
    """没有注入日志回调时保持静默。"""


def _ensure_qt_main_thread(component: str) -> None:
    """Qt 窗口只能在主线程创建；后台线程必须用信号投递。"""
    current = threading.current_thread()
    if current is not threading.main_thread():
        raise RuntimeError(
            f"{component} 必须在 Qt 主线程创建，当前线程：{current.name}"
        )


def qt_scrollbar_width() -> int:
    """滚动条宽度（逻辑像素），按屏幕 DPI 缩放，最小 6px。"""
    screen = QApplication.primaryScreen()
    dpi = screen.logicalDotsPerInch() if screen is not None else 96.0
    scale = min(1.5, max(1.0, float(dpi) / 96.0))
    return max(6, int(round(QT_SCROLLBAR_WIDTH * scale)))


def scrollbar_qss(width: Optional[int] = None) -> str:
    """共享滚动条 QSS：无箭头、无翻页块、圆角浅灰 handle。

    所有颜色/尺寸都来自 ``config``，Qt 模块内不出现硬编码色值。
    """
    bar = qt_scrollbar_width() if width is None else max(1, int(width))
    margin = QT_SCROLLBAR_MARGIN
    radius = max(1, (bar - margin * 2) // 2)
    return (
        f"QScrollBar:vertical {{ background: {QT_SCROLLBAR_GROOVE};"
        f" width: {bar}px; margin: 0px; border: none; }}"
        f"QScrollBar:horizontal {{ background: {QT_SCROLLBAR_GROOVE};"
        f" height: {bar}px; margin: 0px; border: none; }}"
        f"QScrollBar::handle:vertical {{ background: {QT_SCROLLBAR_HANDLE};"
        f" min-height: {QT_SCROLLBAR_MIN_LENGTH}px;"
        f" border-radius: {radius}px; margin: {margin}px; }}"
        f"QScrollBar::handle:horizontal {{ background: {QT_SCROLLBAR_HANDLE};"
        f" min-width: {QT_SCROLLBAR_MIN_LENGTH}px;"
        f" border-radius: {radius}px; margin: {margin}px; }}"
        "QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover"
        f" {{ background: {QT_SCROLLBAR_HANDLE_HOVER}; }}"
        "QScrollBar::handle:vertical:pressed,"
        " QScrollBar::handle:horizontal:pressed"
        f" {{ background: {QT_SCROLLBAR_HANDLE_PRESS}; }}"
        "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,"
        " QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal"
        " { height: 0px; width: 0px; background: transparent; border: none; }"
        "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical,"
        " QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal"
        " { background: transparent; border: none; }"
    )


class PressBounceButton(QPushButton):
    """按压回弹按钮：只在 ``paintEvent`` 内按轴缩放绘制内容，控件 rect 永远不变。

    因为不动 geometry，父布局不会收到 ``LayoutRequest``，也不会覆盖动画；
    QSS 的 normal/hover/pressed/disabled 全部仍由 QStyle 绘制，颜色不被改写。
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

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._press_progress = 0.0
        self._press_anim = QPropertyAnimation(self, b"pressProgress", self)
        self._press_anim.setDuration(PRESS_ANIM_MS)
        self._press_anim.setEasingCurve(QEasingCurve.Type.OutQuad)

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

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 先走默认实现，QSS :pressed 状态照常生效。
        super().mousePressEvent(event)
        if not self.isEnabled() or event.button() != Qt.MouseButton.LeftButton:
            return
        self._animate_press(1.0)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        super().mouseReleaseEvent(event)
        if not self.isEnabled():
            self._snap_release()
            return
        self._animate_press(0.0)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.EnabledChange and not self.isEnabled():
            self._snap_release()

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._press_progress <= 0.001:
            super().paintEvent(event)
            return
        painter = QStylePainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        width = max(1, self.width())
        height = max(1, self.height())
        # 按轴缩放：走满进度时绘制内容在宽/高各缩 PRESS_SHRINK_PX 逻辑像素，
        # 控件 rect 保持不变，所以父布局永远不需要重新排版。
        shrink = PRESS_SHRINK_PX * self._press_progress
        scale_x = max(0.0, 1.0 - shrink / width)
        scale_y = max(0.0, 1.0 - shrink / height)
        cx, cy = width / 2.0, height / 2.0
        painter.translate(cx, cy)
        painter.scale(scale_x, scale_y)
        painter.translate(-cx, -cy)
        option = QStyleOptionButton()
        self.initStyleOption(option)
        painter.drawControl(QStyle.ControlElement.CE_PushButton, option)
        painter.end()


def ensure_qapplication() -> QApplication:
    """返回进程内唯一的 QApplication；必须在主线程首次调用。"""
    _ensure_qt_main_thread("QApplication")
    app = QApplication.instance()
    if app is None:
        # 必须在构造 QApplication 之前设置；Qt6 默认已启用高 DPI pixmap。
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication(["PixFilter"])
        app.setApplicationName("PixFilter")
        app.setQuitOnLastWindowClosed(False)
    # 应用级主题表：所有窗口/滚动区共享同一份滚动条样式，避免逐控件覆盖。
    # 只在内容变化时重设，避免每次取 QApplication 都触发全量重 Polish。
    sheet = scrollbar_qss()
    if getattr(app, "_pixfilter_sheet", None) != sheet:
        app.setStyleSheet(sheet)
        app._pixfilter_sheet = sheet
    return app


def process_qt_events() -> None:
    """由 Tk 的 after 轮询驱动，泵一次 Qt 事件队列。"""
    app = QApplication.instance()
    if app is not None:
        app.processEvents()


def _font(size: int = 10, bold: bool = False) -> QFont:
    font = QFont(FONT_FAMILY, size)
    font.setBold(bold)
    return font


def _load_pixmap(
    path, max_width: int, max_height: int
) -> tuple[QPixmap, int, int]:
    """加载并等比缩放为 QPixmap；失败抛出真实异常供上层写日志。"""
    raw = str(path).strip() if path is not None else ""
    if not raw:
        raise ValueError("图片路径为空")
    source_path = Path(raw).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"图片不存在：{source_path}")

    with Image.open(source_path) as source:
        width, height = source.size
        if width <= 0 or height <= 0:
            raise ValueError(f"图片尺寸无效：{width}x{height}")
        source.load()

    pixmap = QPixmap(str(source_path))
    if pixmap.isNull():
        raise ValueError("QPixmap 解码失败")
    max_width = max(1, int(max_width))
    max_height = max(1, int(max_height))
    if pixmap.width() > max_width or pixmap.height() > max_height:
        scaled = pixmap.scaled(
            QSize(max_width, max_height),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
    else:
        # 小图保持原始像素，不做放大拉伸。
        scaled = pixmap
    if scaled.isNull():
        raise ValueError("QPixmap 缩放失败")
    return scaled, width, height


def _card_style(selected: bool) -> str:
    """卡片基础样式；选中态仅改变边框粗细/颜色与底色，不改尺寸。"""
    if selected:
        bg = REVIEW_CARD_SELECTED_BG
        border = REVIEW_CARD_SELECTED_BORDER
        width = REVIEW_CARD_SELECTED_BORDER_WIDTH
        hover_border = REVIEW_CARD_SELECTED_HOVER_BORDER
        hover_bg = REVIEW_CARD_SELECTED_BG
    else:
        bg = REVIEW_CARD_BG
        border = REVIEW_CARD_BORDER
        width = REVIEW_CARD_BORDER_WIDTH
        hover_border = REVIEW_CARD_HOVER_BORDER
        hover_bg = REVIEW_CARD_HOVER_BG
    return (
        f"QFrame#reviewCard {{ background-color: {bg};"
        f" border: {width}px solid {border};"
        f" border-radius: {REVIEW_CARD_RADIUS}px; }}"
        f"QFrame#reviewCard:hover {{ background-color: {hover_bg};"
        f" border: {width}px solid {hover_border}; }}"
    )


def _styles() -> dict[str, str]:
    """所有颜色、圆角与字体都来自 config，QSS 仅做模板注入。"""
    button_base = (
        f'font-family: "{FONT_FAMILY}";'
        f" font-size: {REVIEW_BUTTON_FONT_SIZE}pt;"
        f" border-radius: {REVIEW_BUTTON_RADIUS}px;"
        f" padding: {REVIEW_BUTTON_PAD_Y}px {REVIEW_BUTTON_PAD_X}px;"
    )
    return {
        "dialog": f"QDialog {{ background-color: {BG}; }}",
        "card": _card_style(False),
        "card_selected": _card_style(True),
        "title": f"color: {TEXT};",
        "muted": f"color: {MUTED};",
        "thumb": (
            f"background-color: {THUMB_BG};"
            f" border-radius: {REVIEW_THUMB_RADIUS}px;"
        ),
        "placeholder": (
            f"background-color: {REVIEW_PLACEHOLDER_BG};"
            f" color: {REVIEW_PLACEHOLDER_FG};"
            f" border: {REVIEW_CARD_BORDER_WIDTH}px solid {REVIEW_CARD_BORDER};"
            f" border-radius: {REVIEW_THUMB_RADIUS}px;"
        ),
        "primary": (
            f"QPushButton {{ background-color: {ACCENT};"
            f" color: {REVIEW_PRIMARY_FG};"
            f" border: {REVIEW_CARD_BORDER_WIDTH}px solid {ACCENT};"
            f" {button_base} }}"
            f"QPushButton:hover {{ background-color: {ACCENT_HOVER};"
            f" border-color: {ACCENT_HOVER}; }}"
            f"QPushButton:pressed {{ background-color: {ACCENT_PRESS};"
            f" border-color: {ACCENT_PRESS}; }}"
            f"QPushButton:disabled {{ background-color: {REVIEW_PRIMARY_DISABLED_BG};"
            f" color: {REVIEW_PRIMARY_DISABLED_FG};"
            f" border-color: {REVIEW_PRIMARY_DISABLED_BG}; }}"
        ),
        "secondary": (
            f"QPushButton {{ background-color: {REVIEW_SECONDARY_BG};"
            f" color: {REVIEW_SECONDARY_FG};"
            f" border: 1px solid {REVIEW_SECONDARY_BORDER};"
            f" {button_base} }}"
            f"QPushButton:hover {{ background-color: {REVIEW_SECONDARY_HOVER_BG};"
            f" border-color: {REVIEW_SECONDARY_HOVER_BORDER}; }}"
            f"QPushButton:pressed {{ background-color: {REVIEW_SECONDARY_PRESS_BG};"
            f" border-color: {REVIEW_SECONDARY_PRESS_BORDER}; }}"
            f"QPushButton:disabled {{ background-color: {REVIEW_SECONDARY_DISABLED_BG};"
            f" color: {REVIEW_SECONDARY_DISABLED_FG};"
            f" border-color: {REVIEW_SECONDARY_DISABLED_BG}; }}"
        ),
        "discard": (
            f"QPushButton {{ background-color: {REVIEW_DISCARD_BG};"
            f" color: {REVIEW_DISCARD_FG};"
            f" border: 1px solid {REVIEW_DISCARD_BORDER};"
            f" {button_base} }}"
            f"QPushButton:hover {{ background-color: {REVIEW_DISCARD_HOVER_BG};"
            f" border-color: {REVIEW_DISCARD_BORDER}; }}"
            f"QPushButton:pressed {{ background-color: {REVIEW_DISCARD_PRESS_BG};"
            f" border-color: {REVIEW_DISCARD_BORDER}; }}"
            f"QPushButton:disabled {{ background-color: {REVIEW_DISCARD_DISABLED_BG};"
            f" color: {REVIEW_DISCARD_DISABLED_FG};"
            f" border-color: {REVIEW_DISCARD_DISABLED_BG}; }}"
        ),
        "scroll": f"QScrollArea {{ border: 0; background-color: {BG}; }}",
        "container": f"background-color: {BG};",
        "radio": (
            f"QRadioButton {{ color: {TEXT}; spacing: 6px;"
            f" padding: {REVIEW_RADIO_PAD}px;"
            f" border: {REVIEW_CARD_BORDER_WIDTH}px solid transparent;"
            f" border-radius: {REVIEW_RADIO_RADIUS}px;"
            f' font-family: "{FONT_FAMILY}";'
            f" font-size: {REVIEW_RADIO_FONT_SIZE}pt; }}"
            f'QRadioButton[selected="true"] {{ font-weight: bold; }}'
            f"QRadioButton:focus {{ background-color: {REVIEW_RADIO_FOCUS_BG};"
            f" border-color: {ACCENT}; }}"
            f"QRadioButton::indicator {{ width: {REVIEW_RADIO_INDICATOR_SIZE}px;"
            f" height: {REVIEW_RADIO_INDICATOR_SIZE}px;"
            f" border: 1px solid {REVIEW_RADIO_BORDER};"
            f" background-color: {REVIEW_RADIO_BG};"
            f" border-radius: {REVIEW_RADIO_INDICATOR_SIZE // 2}px; }}"
            f"QRadioButton::indicator:hover"
            f" {{ border-color: {REVIEW_RADIO_HOVER_BORDER}; }}"
            f"QRadioButton::indicator:checked"
            f" {{ background-color: {REVIEW_RADIO_CHECKED_BG};"
            f" border-color: {REVIEW_RADIO_CHECKED_BG}; }}"
        ),
    }


class _ClickableLabel(QLabel):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class _ClickableFrame(QFrame):
    clicked = Signal()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class QtImageViewer(QDialog):
    """大图窗口：等比缩放到屏幕 80%，QPixmap 由实例强引用。"""

    def __init__(
        self,
        info: ImageInfo,
        log: Optional[Callable[[str, str], None]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        _ensure_qt_main_thread("QtImageViewer")
        self._log = log or _default_log
        self.setWindowTitle(f"查看大图 · {info.path.name}")
        self.setStyleSheet(f"QDialog {{ background-color: {VIEWER_BG}; }}")
        self._pixmap: Optional[QPixmap] = None

        screen = QApplication.primaryScreen()
        geometry = screen.availableGeometry() if screen is not None else None
        max_width = max(
            VIEWER_MIN_WIDTH,
            int((geometry.width() if geometry else 1280) * VIEWER_SCREEN_RATIO),
        )
        max_height = max(
            VIEWER_MIN_HEIGHT,
            int((geometry.height() if geometry else 800) * VIEWER_SCREEN_RATIO),
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        try:
            pixmap, _width, _height = _load_pixmap(info.path, max_width, max_height)
        except _PREVIEW_ERRORS as exc:
            self._log(
                f"[大图预览] 无法加载图片：{info.path}"
                f"（{type(exc).__name__}: {exc}）",
                "warn",
            )
            label = QLabel("无法加载图片\n" f"{info.path.name}\n{type(exc).__name__}")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet(f"color: {VIEWER_TEXT};")
            label.setMinimumSize(VIEWER_MIN_WIDTH, VIEWER_MIN_HEIGHT)
        else:
            self._pixmap = pixmap
            label = QLabel()
            label.setPixmap(pixmap)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label, 1)

        caption = QLabel(
            f"{info.rel_path} · {info.width}×{info.height} · "
            f"{format_size(info.file_size)}"
        )
        caption.setStyleSheet(f"color: {VIEWER_MUTED}; font-size: 9pt;")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(caption)

        self.resize(
            max(label.sizeHint().width() + 24, VIEWER_MIN_WIDTH),
            max(label.sizeHint().height() + 52, VIEWER_MIN_HEIGHT),
        )

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_Space):
            self.close()
            return
        super().keyPressEvent(event)


class QtReviewDialog(QDialog):
    """一组一确认的并排对比窗口；非阻塞 show()，由调用方泵事件。

    结果放在 ``decision``：``(action, keeper)``，action 为
    ``confirm`` / ``skip`` / ``suspend``。X / Esc 等价于 ``suspend``。
    """

    def __init__(
        self,
        group: DuplicateGroup,
        index: int,
        total: int,
        log: Optional[Callable[[str, str], None]] = None,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        _ensure_qt_main_thread("QtReviewDialog")
        self.group = group
        self.members = group_members(group)
        self.default = pick_default_keeper(group)
        self.decision: Optional[tuple[str, ImageInfo]] = None
        self._log = log or _default_log
        self._finished = False
        self._styles = _styles()

        # QPixmap / 缩略图 / 占位图 / 每张的状态文案都需要强引用。
        self._pixmaps: list[QPixmap] = []
        self._thumbnail_labels: list[QLabel] = []
        self._placeholders: list[QLabel] = []
        self._status_labels: dict[str, QLabel] = {}
        self._radios: dict[str, QRadioButton] = {}
        # 卡片本身与阴影 effect 均需强引用：effect 被 GC 会导致阴影消失。
        self._cards: dict[str, _ClickableFrame] = {}
        self._card_effects: list[QGraphicsDropShadowEffect] = []
        # 卡片各自独立父级，必须用 QButtonGroup 保证跨卡片单选互斥。
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)
        self._viewers: list[QtImageViewer] = []

        kind_text = "完全重复" if group.kind == "exact" else "视觉相似"
        self.setWindowTitle(
            f"PixFilter 确认 · 第 {index}/{total} 组 · {kind_text}"
        )
        self.setStyleSheet(self._styles["dialog"])
        self.setMinimumSize(REVIEW_MIN_WIDTH, REVIEW_MIN_HEIGHT)
        self.resize(self._preferred_size())

        self._build(index, total, kind_text)
        self._refresh_status()

    def _preferred_size(self) -> QSize:
        screen = QApplication.primaryScreen()
        geometry = screen.availableGeometry() if screen is not None else None
        screen_w = geometry.width() if geometry else 1280
        screen_h = geometry.height() if geometry else 800
        desired = len(self.members) * REVIEW_CARD_SPACING + REVIEW_EXTRA_WIDTH
        width = min(
            int(screen_w * REVIEW_SCREEN_WIDTH_RATIO),
            max(REVIEW_OPEN_MIN_WIDTH, desired),
        )
        height = min(int(screen_h * REVIEW_SCREEN_HEIGHT_RATIO), REVIEW_MAX_HEIGHT)
        return QSize(width, height)

    def _build(self, index: int, total: int, kind_text: str) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            DIALOG_BODY_PADX, DIALOG_BODY_PADY, DIALOG_BODY_PADX, DIALOG_BODY_PADY
        )
        outer.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel(
            f"第 {index}/{total} 组 · {kind_text} · {len(self.members)} 张"
        )
        title.setFont(_font(14, bold=True))
        title.setStyleSheet(self._styles["title"])
        header.addWidget(title)
        header.addStretch(1)
        if self.group.kind == "similar":
            detail = QLabel(f"最大 pHash 差异 {self.group.max_distance}")
            detail.setStyleSheet(self._styles["muted"])
            header.addWidget(detail)
        outer.addLayout(header)

        hint = QLabel(
            f"请选择要保留的一张，其余会在确认后移入“{TRASH_DIR_NAME}”。"
            "点击缩略图可查看大图。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(self._styles["muted"])
        outer.addWidget(hint)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(self._styles["scroll"])
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        container = QWidget()
        container.setStyleSheet(self._styles["container"])
        row = QHBoxLayout(container)
        # 预留阴影绘制空间；左右等宽，居中时两侧留白仍然对称。
        row.setContentsMargins(
            REVIEW_CARD_SHADOW_PAD, REVIEW_CARD_SHADOW_PAD,
            REVIEW_CARD_SHADOW_PAD,
            REVIEW_CARD_SHADOW_PAD + REVIEW_SHADOW_OFFSET_Y,
        )
        row.setSpacing(DIALOG_CARD_PADX * 2)
        # 首尾 stretch：内容窄于视口时整体居中，超出时 stretch 归零并从左侧滚动。
        row.addStretch(1)
        for info in self.members:
            row.addWidget(self._build_card(info))
        row.addStretch(1)
        scroll.setWidget(container)
        outer.addWidget(scroll, 1)
        self._scroll = scroll
        self._container = container
        self._card_row = row

        footer = QHBoxLayout()
        footer_hint = QLabel(
            "点“跳过”= 本次放弃该组；关闭窗口（X）= 挂起，"
            "可稍后在主界面继续处理。两种都不会移动文件。"
        )
        footer_hint.setWordWrap(True)
        footer_hint.setMaximumWidth(REVIEW_FOOTER_WRAP)
        footer_hint.setStyleSheet(self._styles["muted"])
        footer.addWidget(footer_hint, 1)

        self.btn_skip = QPushButton("跳过")
        self.btn_skip.setStyleSheet(self._styles["secondary"])
        self.btn_skip.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_skip.clicked.connect(lambda: self._finish("skip"))
        footer.addWidget(self.btn_skip)

        self.btn_confirm = QPushButton("保留选中的，移动其他图片")
        self.btn_confirm.setStyleSheet(self._styles["primary"])
        self.btn_confirm.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_confirm.clicked.connect(lambda: self._finish("confirm"))
        footer.addWidget(self.btn_confirm)
        outer.addLayout(footer)

    def _build_card(self, info: ImageInfo) -> QFrame:
        card = _ClickableFrame()
        card.setObjectName("reviewCard")
        card.setProperty("selected", False)
        card.setStyleSheet(self._styles["card"])
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
        layout.setContentsMargins(
            DIALOG_CONTENT_PAD, DIALOG_CONTENT_PAD,
            DIALOG_CONTENT_PAD, DIALOG_CONTENT_PAD,
        )
        layout.setSpacing(4)

        thumb = _ClickableLabel()
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
        thumb.clicked.connect(lambda item=info: self._open_viewer(item))
        layout.addWidget(thumb, 0, Qt.AlignmentFlag.AlignHCenter)

        name = QLabel(info.path.name)
        name.setFont(_font(10, bold=True))
        name.setStyleSheet(self._styles["title"])
        name.setWordWrap(True)
        layout.addWidget(name)

        directory = str(info.rel_path.parent)
        if directory not in ("", "."):
            folder = QLabel(directory)
            folder.setStyleSheet(self._styles["muted"])
            folder.setWordWrap(True)
            layout.addWidget(folder)

        resolution = (
            f"{info.width}×{info.height}" if info.width and info.height else "尺寸未知"
        )
        detail = QLabel(f"{resolution} · {format_size(info.file_size)}")
        detail.setStyleSheet(self._styles["muted"])
        layout.addWidget(detail)

        radio = QRadioButton("保留这一张")
        radio.setStyleSheet(self._styles["radio"])
        radio.setFont(_font(REVIEW_RADIO_FONT_SIZE))
        radio.setProperty("selected", False)
        radio.setCursor(Qt.CursorShape.PointingHandCursor)
        radio.toggled.connect(
            lambda checked, path=str(info.path): self._on_radio(path, checked)
        )
        self._radios[str(info.path)] = radio
        self._button_group.addButton(radio)
        radio.setChecked(str(info.path) == str(self.default.path))
        layout.addWidget(radio)

        status = QLabel()
        status.setStyleSheet(self._styles["muted"])
        self._status_labels[str(info.path)] = status
        layout.addWidget(status)
        layout.addStretch(1)

        card.clicked.connect(lambda item=info: self._select_card(item))
        return card

    def _selected_path(self) -> str:
        for path, radio in self._radios.items():
            if radio.isChecked():
                return path
        return str(self.default.path)

    def _on_radio(self, path: str, checked: bool) -> None:
        if checked:
            self._refresh_status()

    def _select_card(self, info: ImageInfo) -> None:
        """点击卡片（缩略图以外的区域）即选中；已选中时不做多余刷新。"""
        radio = self._radios.get(str(info.path))
        if radio is not None and not radio.isChecked():
            radio.setChecked(True)

    def _refresh_status(self) -> None:
        selected = self._selected_path()
        for path, label in self._status_labels.items():
            if path == selected:
                label.setText("✓ 当前将被保留")
                label.setStyleSheet(f"color: {GREEN};")
            else:
                label.setText(f"→ 确认后移入“{TRASH_DIR_NAME}”")
                label.setStyleSheet(f"color: {AMBER};")
        self._refresh_card_states(selected)

    def _refresh_card_states(self, selected: str) -> None:
        """立即把旧卡片恢复为基础样式，只给当前选中卡片加高亮边框。"""
        for path, card in self._cards.items():
            is_selected = path == selected
            card.setProperty("selected", is_selected)
            card.setStyleSheet(
                self._styles["card_selected" if is_selected else "card"]
            )
            radio = self._radios.get(path)
            if radio is not None:
                font = radio.font()
                font.setBold(is_selected)
                radio.setFont(font)
                radio.setProperty("selected", is_selected)
                radio.style().unpolish(radio)
                radio.style().polish(radio)

    def _open_viewer(self, info: ImageInfo) -> None:
        if self._finished:
            return
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

    def _finish(self, action: str) -> None:
        if self._finished:
            return
        self._finished = True
        selected = next(
            (item for item in self.members if str(item.path) == self._selected_path()),
            self.default,
        )
        self.decision = (action, selected)
        for viewer in self._viewers:
            viewer.close()
        self._viewers.clear()
        self._release_refs()
        super().accept()

    def _release_refs(self) -> None:
        for label in self._thumbnail_labels:
            label.clear()
        self._thumbnail_labels.clear()
        self._pixmaps.clear()
        self._placeholders.clear()

    def reject(self) -> None:
        """X / Esc：挂起，而不是跳过。"""
        self._finish("suspend")

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._finished:
            self._finish("suspend")
        event.accept()


class QtReviewBridge(QObject):
    """把服务回调从工作线程安全投递到 Qt 主线程。

    服务的 ``group_ready`` 可能来自 ``threading.Thread``；
    ``post`` 可在任意线程调用，信号以 QueuedConnection 落到主线程。
    """

    groupReady = Signal(object, int, int, object)

    def __init__(self, handler: Callable[[object, int, int, object], None], parent=None):
        super().__init__(parent)
        _ensure_qt_main_thread("QtReviewBridge")
        self._handler = handler
        self.groupReady.connect(
            self._dispatch, Qt.ConnectionType.QueuedConnection
        )

    def post(self, group, index: int, total: int, root) -> None:
        self.groupReady.emit(group, index, total, root)

    @Slot(object, int, int, object)
    def _dispatch(self, group, index: int, total: int, root) -> None:
        _ensure_qt_main_thread("QtReviewBridge 槽")
        self._handler(group, index, total, root)
