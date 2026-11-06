"""Toast 圆角/透明诊断：对真实渲染结果逐像素采样。

用法：
    python tests/diag_toast_rounding.py
        # 默认 offscreen，不弹窗、不动光标，输出标注「offscreen 非真机证据」
    python tests/diag_toast_rounding.py --real
        # 真实平台（Windows 用 windows 插件），会创建真实窗口、可能占用桌面/焦点

每个缩放档都会在独立子进程里重建 QApplication（Qt 的缩放因子只能在启动时生效）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCALES = ("1", "1.25", "2")
LONG_TEXT = (
    "这是一条非常长的提示文案，用来确认 Toast 宽度不会超过上限并且文字会被省略。"
    "这是第二条用于加长的句子。这是第三条用于加长的句子。"
)


def _green(color) -> bool:
    return (
        color.alpha() > 160
        and color.green() >= 120
        and color.red() <= 130
        and color.blue() <= 140
    )


def _child() -> int:
    sys.path.insert(0, str(ROOT))

    from PySide6.QtGui import QGuiApplication

    import config
    import ui_qt_main

    app = ui_qt_main.ensure_qapplication()
    platform = QGuiApplication.platformName()

    window = ui_qt_main.PixFilterQtWindow()
    if platform == "offscreen":
        from PySide6.QtCore import Qt

        window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    window.show()
    deadline = time.monotonic() + 0.4
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.004)

    text = "扫描完成，已处理 12 个文件"
    window.show_toast(text)
    toast = window._toast
    assert toast is not None
    deadline = time.monotonic() + 0.6
    while time.monotonic() < deadline and toast.pos() != toast._end_pos:
        app.processEvents()
        time.sleep(0.004)
    app.processEvents()

    pixmap = toast.grab()
    image = pixmap.toImage()
    ratio = pixmap.devicePixelRatio() or 1.0
    width, height = image.width(), image.height()

    def at(x: int, y: int):
        return image.pixelColor(x, y)

    corners = {
        "tl": at(0, 0),
        "tr": at(width - 1, 0),
        "bl": at(0, height - 1),
        "br": at(width - 1, height - 1),
    }
    center = at(width // 2, height // 2)

    top_inset = next(
        (x for x in range(width) if _green(at(x, 0))), width
    )
    left_inset = next(
        (y for y in range(height) if _green(at(0, y))), height
    )
    diagonal_inset = next(
        (k for k in range(min(width, height)) if _green(at(k, k))), -1
    )

    xs: list[int] = []
    ys: list[int] = []
    colors: dict[tuple[int, int, int, int], int] = {}
    for y in range(height):
        for x in range(width):
            color = at(x, y)
            if _green(color):
                xs.append(x)
                ys.append(y)
            if color.alpha() > 250:
                key = (color.red(), color.green(), color.blue(), color.alpha())
                colors[key] = colors.get(key, 0) + 1
    bbox = (
        (min(xs), min(ys), max(xs), max(ys)) if xs else (None, None, None, None)
    )
    dominant = max(colors, key=colors.get) if colors else (0, 0, 0, 0)

    def rgba(color) -> list[int]:
        return [color.red(), color.green(), color.blue(), color.alpha()]

    # 文字会占满中线，因此“背景绿”改采左右内边距与顶部内边距三个安全点。
    mid_y = height // 2
    fill_samples = {
        "left_pad": rgba(at(6, mid_y)),
        "right_pad": rgba(at(width - 7, mid_y)),
        "top_pad": rgba(at(width // 2, 4)),
    }

    frame = toast._frame
    frame_geometry = (
        frame.geometry().x(),
        frame.geometry().y(),
        frame.geometry().width(),
        frame.geometry().height(),
    )

    result = {
        "platform": platform,
        "scale": float(os.environ.get("QT_SCALE_FACTOR", "1")),
        "dpr": float(ratio),
        "window_size": [toast.width(), toast.height()],
        "pixmap_size": [width, height],
        "frame_geometry": list(frame_geometry),
        "frame_radius": float(config.TOAST_RADIUS),
        "corners": {key: rgba(value) for key, value in corners.items()},
        "center": rgba(center),
        "dominant": list(dominant),
        "fill_samples": fill_samples,
        "top_inset_px": round(top_inset / ratio, 2),
        "left_inset_px": round(left_inset / ratio, 2),
        "diagonal_inset_px": round(diagonal_inset / ratio, 2)
        if diagonal_inset >= 0
        else None,
        "green_bbox": list(bbox),
        "green_pixels": len(xs),
        "green_outside_window": bool(
            xs
            and (bbox[0] < 0 or bbox[1] < 0 or bbox[2] > width - 1 or bbox[3] > height - 1)
        ),
        "label_text": toast._label.text(),
    }
    print("TOAST_RESULT " + json.dumps(result, ensure_ascii=False))
    toast.close()
    window.close()
    app.processEvents()
    return 0


def _parent() -> int:
    real = "--real" in sys.argv
    platform = os.environ.get("QT_QPA_PLATFORM")
    if real:
        platform = "windows" if sys.platform.startswith("win") else (platform or "xcb")
        print(
            "警告：--real 会创建真实窗口，可能抢占桌面焦点；默认请用 offscreen。",
            file=sys.stderr,
        )
    elif not platform:
        platform = "offscreen"
    if platform == "offscreen":
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    print(f"platform={platform}")
    if platform == "offscreen":
        print("注意：offscreen 非真机证据，仅用于无显示环境的结构检查。")
    print("-" * 72)
    failures = 0
    for scale in SCALES:
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = platform
        env["QT_SCALE_FACTOR"] = scale
        env["PYTHONPATH"] = str(ROOT)
        command = [sys.executable, str(Path(__file__).resolve()), "--child"]
        if real:
            command.append("--real")
        completed = subprocess.run(
            command,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        line = next(
            (row for row in completed.stdout.splitlines() if row.startswith("TOAST_RESULT ")),
            None,
        )
        if completed.returncode != 0 or line is None:
            failures += 1
            print(f"scale={scale} 失败\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}")
            continue
        data = json.loads(line[len("TOAST_RESULT "):])
        print(
            f"scale={data['scale']:g} dpr={data['dpr']:g}"
            f" window={data['window_size']} pixmap={data['pixmap_size']}"
            f" frame={data['frame_geometry']}"
        )
        print(
            "  corners(alpha)="
            + " ".join(
                f"{key}:{tuple(value)}" for key, value in data["corners"].items()
            )
        )
        print(
            f"  center={tuple(data['center'])}"
            f" dominant={tuple(data['dominant'])}"
            f" fill_samples={ {k: tuple(v) for k, v in data['fill_samples'].items()} }"
        )
        print(
            f"  top_inset={data['top_inset_px']}px"
            f" left_inset={data['left_inset_px']}px"
            f" diagonal_inset={data['diagonal_inset_px']}px"
        )
        print(
            f"  green_bbox={data['green_bbox']} green_pixels={data['green_pixels']}"
            f" outside_window={data['green_outside_window']}"
            f" label={data['label_text']!r}"
        )
    print("-" * 72)
    print("OK" if failures == 0 else f"FAIL {failures} 个缩放档")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    if "--child" in sys.argv:
        raise SystemExit(_child())
    raise SystemExit(_parent())
