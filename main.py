"""PixFilter 程序入口：无条件启动 Qt（PySide6）界面。

界面布局见 ui_qt_main.py，业务编排见 scan_service.py，
扫描与去重逻辑见 pixfilter_core.py。本项目不再包含任何 Tk 后端。
"""

from __future__ import annotations

import sys

from platform_support import prepare_windows_shell

QT_MISSING_MESSAGE = (
    "PixFilter 需要 PySide6 才能启动：当前环境未检测到可用的 PySide6。"
    "请执行 pip install -r requirements.txt 后重试。"
)


def _run_qt() -> int:
    """导入 Qt 并进入主事件循环；返回进程退出码。"""
    import PySide6  # noqa: F401  # 缺失时由 main() 统一报错退出。

    from ui_qt_main import run_qt_app

    return run_qt_app()


def main() -> int:
    # 任务栏图标身份必须在任何窗口创建之前设置。
    prepare_windows_shell()
    try:
        return _run_qt()
    except ImportError as exc:
        print(
            f"{QT_MISSING_MESSAGE}（{type(exc).__name__}: {exc}）",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
