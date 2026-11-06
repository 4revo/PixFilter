"""诊断「待处理重复组」面板右上角的灰色竖线由谁绘制。

用法（独立运行，默认 offscreen，不弹窗、不动光标）：

    python tests/diag_pending_line.py              # 空态 + 18 组溢出态都扫描
    python tests/diag_pending_line.py --real       # 真实平台（会弹窗、占用桌面）
    python tests/diag_pending_line.py --real --onscreen
    python tests/diag_pending_line.py --offscreen

输出内容：
- 面板右上区域内的非背景像素：颜色、坐标范围、最长竖直游程；
- 该像素命中的控件类名、objectName、geometry、frameShape/lineWidth 等；
- 表头/角按钮/表格视口/滚动条的几何与边框属性。

修复前（旧版）典型输出：在 `pending_count_label`（QLabel，8x18）右缘出现
`#e2e7f1` 的 1px×18px 竖线——它来自卡片 QSS 里泛化的 `QFrame` 边框规则
层叠到所有 QFrame 子类（QLabel / QStackedWidget）。
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REAL = "--real" in sys.argv
if REAL:
    if sys.platform.startswith("win"):
        os.environ["QT_QPA_PLATFORM"] = "windows"
    print(
        "警告：--real 会创建真实窗口，可能抢占桌面焦点；默认请用 offscreen。",
        file=sys.stderr,
    )
else:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QFrame, QTableWidgetItem, QWidget  # noqa: E402

import config  # noqa: E402
import ui_qt_main  # noqa: E402

BACKGROUNDS = {config.CARD.lower(), config.PENDING_EMPTY_BG.lower(), config.BG.lower()}


def _hex(color: QColor) -> str:
    return f"#{color.red():02x}{color.green():02x}{color.blue():02x}"


def _desc(widget: QWidget | None) -> str:
    if widget is None:
        return "<none>"
    extra = ""
    if isinstance(widget, QFrame):
        extra = (
            f" frameShape={widget.frameShape().name}"
            f" lineWidth={widget.lineWidth()}"
            f" midLineWidth={widget.midLineWidth()}"
        )
    rect = widget.geometry()
    return (
        f"{type(widget).__name__}#{widget.objectName()}"
        f" geom=({rect.x()},{rect.y()},{rect.width()},{rect.height()})"
        f" visible={widget.isVisible()}{extra}"
    )


def _chain(root: QWidget, point: QPoint) -> str:
    """从根控件逐层 childAt，输出该点的命中链。"""
    parts: list[str] = []
    current: QWidget | None = root
    local = point
    for _ in range(12):
        if current is None:
            break
        parts.append(_desc(current))
        child = current.childAt(local)
        if child is None or child is current:
            break
        local = child.mapFrom(current, local)
        current = child
    return " -> ".join(parts)


def _scan(pixmap, rect: QRect) -> dict:
    """扫描区域内非背景像素，按颜色聚类并给出边界与最长竖直游程。"""
    image = pixmap.toImage()
    ratio = pixmap.devicePixelRatio() or 1.0
    hits: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for y in range(rect.top(), rect.bottom() + 1):
        for x in range(rect.left(), rect.right() + 1):
            px = int(round(x * ratio))
            py = int(round(y * ratio))
            if not (0 <= px < image.width() and 0 <= py < image.height()):
                continue
            value = _hex(image.pixelColor(px, py))
            if value in BACKGROUNDS:
                continue
            hits[value].append((x, y))

    report: dict[str, dict] = {}
    for value, points in hits.items():
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        by_x: dict[int, list[int]] = defaultdict(list)
        for x, y in points:
            by_x[x].append(y)
        best = (0, 0, 0)
        for x, column in by_x.items():
            column.sort()
            start = prev = column[0]
            for y in column[1:]:
                if y == prev + 1:
                    prev = y
                    continue
                if prev - start + 1 > best[0]:
                    best = (prev - start + 1, x, start)
                start = prev = y
            if prev - start + 1 > best[0]:
                best = (prev - start + 1, x, start)
        report[value] = {
            "count": len(points),
            "x": (min(xs), max(xs)),
            "y": (min(ys), max(ys)),
            "longest_run": best,
        }
    return report


def _fill_table(window: ui_qt_main.PixFilterQtWindow, rows: int) -> None:
    window.pending_stack.setCurrentWidget(window.pending_table)
    window.pending_table.setRowCount(rows)
    for row in range(rows):
        for column in range(window.pending_table.columnCount()):
            item = window.pending_table.item(row, column) or QTableWidgetItem()
            item.setText(f"r{row}c{column}")
            window.pending_table.setItem(row, column, item)


def _report_region(window, pixmap, label: str, region: QRect, tag: str) -> dict:
    print(f"[{label}/{tag}] scan region={region.getRect()}")
    report = _scan(pixmap, region)
    if not report:
        print(f"[{label}/{tag}] 区域内无非背景像素")
    for value, info in sorted(report.items(), key=lambda kv: -kv[1]["count"]):
        print(
            f"[{label}/{tag}] color={value} count={info['count']}"
            f" x={info['x'][0]}..{info['x'][1]} y={info['y'][0]}..{info['y'][1]}"
            f" longest_vertical_run={info['longest_run']}"
        )
        run_len, run_x, run_y = info["longest_run"]
        if run_len >= 2:
            probe = QPoint(run_x, run_y + run_len // 2)
            print(f"[{label}/{tag}]   probe=({probe.x()},{probe.y()})")
            print(f"[{label}/{tag}]   chain: {_chain(window, probe)}")
    return report


def _report(window, app, label: str, span: int) -> None:
    target = (
        window.pending_stack
        if window.pending_stack.currentWidget() is window.pending_table
        else window.pending_empty
    )
    origin = target.mapTo(window, QPoint(0, 0))
    right = origin.x() + target.width()
    pixmap = window.grab()
    print(
        f"\n[{label}] platform={QGuiApplication.platformName()!r}"
        f" grab={pixmap.width()}x{pixmap.height()}"
        f" dpr={pixmap.devicePixelRatio()}"
    )
    print(
        f"[{label}] panel={type(target).__name__}"
        f" rect=({origin.x()},{origin.y()},{target.width()},{target.height()})"
    )
    card = window.pending_stack.parentWidget()
    card_origin = card.mapTo(window, QPoint(0, 0))
    stack_origin = window.pending_stack.mapTo(window, QPoint(0, 0))
    print(
        f"[{label}] stack=({stack_origin.x()},{stack_origin.y()},"
        f"{window.pending_stack.width()},{window.pending_stack.height()})"
        f" card=({card_origin.x()},{card_origin.y()},"
        f"{card.width()},{card.height()})"
        f" card_right_edge={card_origin.x() + card.width() - 1}"
    )

    # 1) 面板内容区右上角（stack 顶部起，旧的？区域）
    content = QRect(max(0, right - span), max(0, origin.y()), span, span)
    _report_region(window, pixmap, label, content, "content")

    # 2) 卡片头部右上角（真正“极短竖线”出现的位置：标题行 + 计数标签）
    card_right = card_origin.x() + card.width() - 1
    header = QRect(max(0, card_right - span + 1), max(0, card_origin.y()), span, span)
    _report_region(window, pixmap, label, header, "header")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--onscreen", action="store_true")
    parser.add_argument("--offscreen", action="store_true")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--span", type=int, default=48)
    args = parser.parse_args()

    app = ui_qt_main.ensure_qapplication()
    window = ui_qt_main.PixFilterQtWindow()
    if not (REAL and args.onscreen):
        # 默认离屏：不使用“移出屏幕”的假静默，直接不创建可见窗口。
        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.resize(config.WINDOW_WIDTH, config.WINDOW_HEIGHT)
    window.show()
    for _ in range(40):
        app.processEvents()

    _report(window, app, "empty", args.span)
    _fill_table(window, 18)
    for _ in range(25):
        app.processEvents()
    _report(window, app, "18-groups", args.span)

    # 旧 bug 的精确位置：计数标签自身矩形外扩 1px（历史实测 x=994, y=508..525）。
    label = window.pending_count_label
    label_origin = label.mapTo(window, QPoint(0, 0))
    print(
        f"\n[count-label] rect=({label_origin.x()},{label_origin.y()},"
        f"{label.width()},{label.height()}) text={label.text()!r}"
    )
    box = QRect(
        label_origin.x() - 1,
        label_origin.y() - 1,
        label.width() + 2,
        label.height() + 2,
    )
    report = _report_region(window, window.grab(), "count-label", box, "box")
    border = report.get(config.BORDER.lower())
    print(
        f"[count-label] border_pixels={border['count'] if border else 0}"
        f" longest_vertical_run={border['longest_run'] if border else None}"
    )

    table = window.pending_table
    print("\n[widgets]")
    print(f"  table frameShape={table.frameShape().name}"
          f" lineWidth={table.lineWidth()} midLineWidth={table.midLineWidth()}")
    header = table.horizontalHeader()
    print(f"  header frameShape={header.frameShape().name}"
          f" rect=({header.geometry().x()},{header.geometry().y()},"
          f"{header.geometry().width()},{header.geometry().height()})")
    corner = table.findChild(QWidget, "qt_scrollarea_cornerwidget")
    print(f"  corner={corner}")
    bar = table.verticalScrollBar()
    print(f"  vscrollbar width={bar.width()} visible={bar.isVisible()}"
          f" maximum={bar.maximum()}")
    print(f"  count_label rect={window.pending_count_label.geometry().getRect()}"
          f" text={window.pending_count_label.text()!r}")

    window.close()
    for _ in range(10):
        app.processEvents()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
