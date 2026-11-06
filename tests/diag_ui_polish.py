"""UI 打磨诊断：按钮按压回弹 / 表格整行 hover / 长文件名截断。

独立运行：``python tests/diag_ui_polish.py``（默认 offscreen，不弹窗、不动光标）。
只有显式传 ``--real`` 才使用真实平台，并会创建真实窗口（可能占用桌面/焦点）。

输出三段：
1. 按钮按下/松开时绘制内容包围盒与控件 rect 变化（含布局请求次数）；
2. 表格 hover 行 5 列背景采样与局部重绘计数；
3. 长文件名场景下文件名区与文件大小区的 x 区间。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

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

import qt_main_cases as cases  # noqa: E402

from PySide6.QtGui import QGuiApplication  # noqa: E402


def main() -> None:
    cases.app()
    platform = QGuiApplication.platformName()
    real = platform != "offscreen"
    print(f"== platform={platform!r} real={real} ==")
    if not real:
        print("注意：offscreen 结果属于“非真机证据”，仅用于数值诊断。")

    print("-- 1) 按钮按下 / 松开 --")
    cases.case_press_bounce()

    print("-- 2) 表格整行 hover --")
    cases.case_table_hover()

    print("-- 3) 长文件名 vs 文件大小 --")
    cases.case_long_name()

    print(f"== DIAG DONE platform={platform!r} ==")


if __name__ == "__main__":
    main()
