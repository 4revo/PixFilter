"""Windows 进程级准备（AppUserModelID）。

Qt6 使用原生高 DPI 缩放，DPI 适配由 Qt 自己完成，本模块不再包含
任何 GUI 框架专属的缩放/字体逻辑。
"""

from __future__ import annotations

import ctypes
import sys

from config import APP_USER_MODEL_ID


def prepare_windows_shell() -> str:
    """设置任务栏独立图标所需的 AppUserModelID；必须在窗口创建前调用。"""
    if sys.platform != "win32":
        return "skip:non-windows"
    try:
        windll = ctypes.windll
    except AttributeError:
        return "skip:no-windll"
    try:
        windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
        return "ok"
    except Exception:
        return "failed"
