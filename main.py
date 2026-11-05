"""PixFilter 图形界面入口。

运行：python main.py
界面只负责展示与交互；扫描和去重逻辑全部在 pixfilter_core.py。
"""

from __future__ import annotations

import ctypes
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, scrolledtext, ttk

from pixfilter_core import (
    DEFAULT_DISTANCE,
    DEFAULT_HASH_SIZE,
    MISSING_KEEPER_ERROR,
    TRASH_DIR_NAME,
    ScanReport,
    SuspendedGroup,
    apply_review_decision,
    group_members,
    pending_groups,
    pick_default_keeper,
    scan_folder,
)
from review_ui import ReviewDialog, Toast

# 统一浅色主题
BG = "#f4f6fb"
CARD = "#ffffff"
BORDER = "#e2e7f1"
TEXT = "#202536"
MUTED = "#7b849b"
ACCENT = "#3d6df2"
ACCENT_HOVER = "#315fdd"
ACCENT_PRESS = "#254fc4"
ACCENT_SOFT = "#eaf0ff"
GREEN = "#177a4c"
AMBER = "#a86500"
RED = "#c0392b"
PURPLE = "#5a3fd0"
LOG_BG = "#fbfcff"

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 20, "bold")
FONT_LOG = ("Microsoft YaHei UI", 9)


def _enable_windows_dpi_awareness() -> str:
    """在创建 Tk 窗口之前启用 Windows DPI 感知。

    返回实际采用的方案（供测试/日志使用）；非 Windows 平台安全跳过。
    Windows 上该设置对进程只能成功一次，重复调用会抛 OSError，
    这里捕获后继续尝试旧版接口，绝不因 DPI 设置失败而崩溃。
    """
    if sys.platform != "win32":
        return "skip:non-windows"
    try:
        windll = ctypes.windll
    except AttributeError:
        return "skip:no-windll"

    try:
        windll.shcore.SetProcessDpiAwareness(1)
        return "shcore"
    except OSError:
        # 已设置过或当前系统不支持 shcore。
        pass
    except Exception:
        pass

    try:
        windll.user32.SetProcessDPIAware()
        return "user32"
    except Exception:
        return "skip:failed"


def configure_dpi_scaling(root: tk.Misc) -> float:
    """按实际 DPI 设置 tk scaling，并把默认字体统一到 10/11pt。

    返回 UI 缩放系数（96 DPI = 1.0），供窗口尺寸、Treeview 行高等
    像素单位换算；缩放系数为 1.0 时保持原有观感。
    """
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:
        dpi = 96.0
    if not 0 < dpi <= 1000:
        dpi = 96.0

    # tk scaling 的单位是“每磅像素数”，所以是 dpi / 72；
    # 点值字体会随 scaling 放大，不能再乘一次系数，否则会变成巨无霸。
    try:
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:
        pass

    # 统一默认字体，避免出现“某些地方字大某些地方字小”。
    for name, size in (
        ("TkDefaultFont", 10),
        ("TkTextFont", 10),
        ("TkHeadingFont", 11),
        ("TkMenuFont", 10),
        ("TkCaptionFont", 10),
        ("TkSmallCaptionFont", 9),
        ("TkIconFont", 10),
        ("TkTooltipFont", 9),
    ):
        try:
            tkfont.nametofont(name).configure(size=size)
        except Exception:
            continue

    scale = dpi / 96.0
    try:
        root._pixfilter_ui_scale = scale  # type: ignore[attr-defined]
    except Exception:
        pass
    return scale


class FlatButton(tk.Button):
    """带悬停/按下反馈的扁平按钮，避免系统默认灰色外观。"""

    def __init__(self, master: tk.Misc, text: str, command, *, primary: bool = False, **kwargs):
        self._primary = primary
        self._normal_bg = ACCENT if primary else CARD
        self._normal_fg = "#ffffff" if primary else TEXT
        self._hover_bg = ACCENT_HOVER if primary else ACCENT_SOFT
        self._press_bg = ACCENT_PRESS if primary else "#dfe7ff"
        self._disabled_bg = "#dfe6f5" if primary else "#f2f4fa"
        self._disabled_fg = "#9aa3ba"

        super().__init__(
            master,
            text=text,
            command=command,
            font=FONT_UI,
            bg=self._normal_bg,
            fg=self._normal_fg,
            activebackground=self._hover_bg,
            activeforeground=self._normal_fg,
            disabledforeground=self._disabled_fg,
            relief="flat",
            bd=0,
            padx=18,
            pady=8,
            cursor="hand2",
            highlightthickness=1,
            highlightbackground=ACCENT if primary else BORDER,
            highlightcolor=ACCENT if primary else BORDER,
            takefocus=True,
            **kwargs,
        )
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)

    def _enabled(self) -> bool:
        return str(self.cget("state")) != "disabled"

    def _on_enter(self, _event=None) -> None:
        if self._enabled():
            self.configure(bg=self._hover_bg)

    def _on_leave(self, _event=None) -> None:
        if self._enabled():
            self.configure(bg=self._normal_bg)

    def _on_press(self, _event=None) -> None:
        if self._enabled():
            self.configure(bg=self._press_bg)

    def _on_release(self, _event=None) -> None:
        if not self._enabled():
            return
        pointer_x, pointer_y = self.winfo_pointerxy()
        inside = (
            0 <= pointer_x - self.winfo_rootx() < self.winfo_width()
            and 0 <= pointer_y - self.winfo_rooty() < self.winfo_height()
        )
        self.configure(bg=self._hover_bg if inside else self._normal_bg)

    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self.configure(
                state="normal",
                bg=self._normal_bg,
                fg=self._normal_fg,
                activebackground=self._hover_bg,
                cursor="hand2",
            )
        else:
            self.configure(
                state="disabled",
                bg=self._disabled_bg,
                fg=self._disabled_fg,
                activebackground=self._disabled_bg,
                cursor="arrow",
            )


class PixFilterApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PixFilter · 照片去重")
        self.root.configure(bg=BG)
        self._ui_scale = float(getattr(self.root, "_pixfilter_ui_scale", 1.0))
        self.root.minsize(
            int(round(900 * self._ui_scale)), int(round(740 * self._ui_scale))
        )

        self._folder: Path | None = None
        self._queue: "queue.Queue[tuple]" = queue.Queue()
        self._decision_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self._active_dialog: ReviewDialog | None = None
        self._stop_event = threading.Event()
        self._scanning = False
        self._closed = False
        self._poll_id: str | None = None
        self._pending_groups: list[SuspendedGroup] = []
        self._pending_uid = 0
        self._pending_busy = False
        self._scan_completed = False
        self._last_report: ScanReport | None = None
        self._toast: Toast | None = None
        self._path_tag_seq = 0

        self.path_var = tk.StringVar(value="尚未选择文件夹")
        self.threshold_var = tk.DoubleVar(value=float(DEFAULT_DISTANCE))
        self.threshold_value = int(DEFAULT_DISTANCE)
        self.threshold_text = tk.StringVar(value=f"当前: {self.threshold_value}")
        self.status_var = tk.StringVar(value="就绪 · 请选择要扫描的文件夹")

        self._build_ui()
        self._center_window()
        self._append(
            "PixFilter 已就绪。选择文件夹后点击“开始扫描”。\n"
            "完全重复按 MD5 识别；相似图片按 pHash 识别。\n"
            f"发现分组后会逐组弹窗确认；点“跳过”= 放弃该组，点 X = 挂起并留在“待处理重复组”。\n"
            f"只有点确认，才会把多余图片移入“{TRASH_DIR_NAME}”。",
            "info",
        )

    # ---------- 界面搭建 ----------
    def _center_window(self) -> None:
        width = int(round(1040 * self._ui_scale))
        height = int(round(860 * self._ui_scale))
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _card(self, parent: tk.Misc) -> tk.Frame:
        return tk.Frame(
            parent,
            bg=CARD,
            highlightbackground=BORDER,
            highlightthickness=1,
            bd=0,
        )

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "PixFilter.Horizontal.TProgressbar",
            thickness=max(4, int(round(6 * self._ui_scale))),
            troughcolor="#e7ecf7",
            background=ACCENT,
            bordercolor="#e7ecf7",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )
        style.configure(
            "PixFilter.Horizontal.TScale",
            background=BG,
            troughcolor="#e2e8f5",
            bordercolor=BORDER,
            lightcolor=ACCENT,
            darkcolor=ACCENT,
        )
        style.configure(
            "Treeview",
            background=CARD,
            fieldbackground=CARD,
            foreground=TEXT,
            rowheight=max(20, int(round(26 * self._ui_scale))),
            borderwidth=0,
            font=FONT_SMALL,
        )
        style.configure(
            "Treeview.Heading",
            background="#eef2fa",
            foreground=TEXT,
            relief="flat",
            font=FONT_SMALL,
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT_SOFT)],
            foreground=[("selected", TEXT)],
        )

        outer = tk.Frame(self.root, bg=BG, padx=26, pady=20)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        tk.Label(
            header,
            text="PixFilter 照片去重",
            font=FONT_TITLE,
            bg=BG,
            fg=TEXT,
            anchor="w",
        ).pack(side="left")
        tk.Label(
            header,
            text="纯本地运行 · 精确重复 + 视觉相似 · 只移动，不删除",
            font=FONT_SMALL,
            bg=BG,
            fg=MUTED,
        ).pack(side="left", padx=(14, 0), pady=(6, 0))

        # 选目录：只保留一个紧凑按钮，靠左上角
        folder_row = tk.Frame(outer, bg=BG)
        folder_row.pack(fill="x", pady=(16, 8))
        self.btn_choose_folder = FlatButton(
            folder_row, "点击此处选择扫描文件夹", self._choose_folder
        )
        self.btn_choose_folder.pack(anchor="w", padx=10, pady=5)

        # 操作区
        action_row = tk.Frame(outer, bg=BG)
        action_row.pack(fill="x", pady=(0, 12))

        threshold_box = tk.Frame(action_row, bg=BG)
        threshold_box.pack(side="left", anchor="n")
        self.threshold_row = tk.Frame(threshold_box, bg=BG)
        self.threshold_row.pack(anchor="w")
        tk.Label(
            self.threshold_row, text="相似阈值", font=FONT_BOLD, bg=BG, fg=TEXT
        ).pack(side="left")
        self.threshold_scale = ttk.Scale(
            self.threshold_row,
            from_=1,
            to=20,
            orient="horizontal",
            length=max(120, int(round(200 * self._ui_scale))),
            variable=self.threshold_var,
            command=self._on_threshold_change,
            style="PixFilter.Horizontal.TScale",
        )
        self.threshold_scale.pack(side="left", padx=(10, 10))
        self.threshold_value_label = tk.Label(
            self.threshold_row,
            textvariable=self.threshold_text,
            font=FONT_UI,
            bg=BG,
            fg=TEXT,
            width=7,
            anchor="w",
        )
        self.threshold_value_label.pack(side="left")
        self.threshold_help_label = tk.Label(
            threshold_box,
            text=f"（pHash {DEFAULT_HASH_SIZE}x{DEFAULT_HASH_SIZE}，默认 {DEFAULT_DISTANCE}；越低越严格）",
            font=("Microsoft YaHei UI", 9),
            bg=BG,
            fg="#888888",
            anchor="w",
        )
        self.threshold_help_label.pack(anchor="w", pady=(2, 0))

        buttons = tk.Frame(action_row, bg=BG)
        buttons.pack(side="right", anchor="n")
        self.btn_stop = FlatButton(buttons, "停止", self._stop_scan)
        self.btn_stop.pack(side="right", padx=(10, 0))
        self.btn_start = FlatButton(buttons, "开始扫描", self._start_scan, primary=True)
        self.btn_start.pack(side="right")
        self.btn_stop.set_enabled(False)

        status_row = tk.Frame(outer, bg=BG)
        status_row.pack(fill="x", pady=(0, 8))
        self.status_label = tk.Label(
            status_row,
            textvariable=self.status_var,
            font=FONT_SMALL,
            bg=BG,
            fg=MUTED,
            anchor="w",
        )
        self.status_label.pack(side="left")
        self.progress = ttk.Progressbar(
            status_row,
            mode="indeterminate",
            length=150,
            style="PixFilter.Horizontal.TProgressbar",
        )

        # 日志卡片
        log_card = self._card(outer)
        log_card.pack(fill="both", expand=True)
        log_head = tk.Frame(log_card, bg=CARD)
        log_head.pack(fill="x", padx=18, pady=(12, 8))
        tk.Label(
            log_head, text="扫描日志", font=FONT_BOLD, bg=CARD, fg=TEXT, anchor="w"
        ).pack(side="left")
        tk.Label(
            log_head,
            text="保留与确认后移入“重复文件”的路径都会记录在这里",
            font=FONT_SMALL,
            bg=CARD,
            fg=MUTED,
        ).pack(side="right")

        self.log_text = scrolledtext.ScrolledText(
            log_card,
            wrap="word",
            height=8,
            font=FONT_LOG,
            bg=LOG_BG,
            fg=TEXT,
            relief="flat",
            bd=0,
            padx=16,
            pady=10,
            state="disabled",
            cursor="arrow",
            highlightthickness=0,
        )
        self.log_text.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        self.log_text.vbar.configure(
            bg="#cdd4e4",
            troughcolor=CARD,
            activebackground="#aeb8cc",
            relief="flat",
            bd=0,
            width=12,
        )
        for tag, color, font in (
            ("info", "#586174", FONT_LOG),
            ("group", ACCENT, FONT_BOLD),
            ("keep", GREEN, FONT_LOG),
            ("move", AMBER, FONT_LOG),
            ("warn", AMBER, FONT_LOG),
            ("skip", "#C62828", FONT_BOLD),
            ("error", RED, FONT_BOLD),
            ("summary", PURPLE, FONT_BOLD),
        ):
            self.log_text.tag_configure(tag, foreground=color, font=font)
        self.log_text.tag_configure("path_link", foreground="#2f6dbd", underline=True)
        self.log_text.tag_configure("copy_ok", foreground=GREEN, font=FONT_BOLD)

        # 待处理重复组面板
        pending_card = self._card(outer)
        pending_card.pack(fill="x", pady=(12, 0))
        pending_inner = tk.Frame(pending_card, bg=CARD, padx=18, pady=12)
        pending_inner.pack(fill="x")

        pending_head = tk.Frame(pending_inner, bg=CARD)
        pending_head.pack(fill="x")
        tk.Label(
            pending_head,
            text="待处理重复组",
            font=FONT_BOLD,
            bg=CARD,
            fg=TEXT,
            anchor="w",
        ).pack(side="left")
        tk.Label(
            pending_head,
            text="对比窗点 X 会挂起到这里，可稍后继续处理或放弃",
            font=FONT_SMALL,
            bg=CARD,
            fg=MUTED,
            anchor="w",
        ).pack(side="left", padx=(12, 0))
        self.pending_count_var = tk.StringVar(value="")
        tk.Label(
            pending_head,
            textvariable=self.pending_count_var,
            font=FONT_SMALL,
            bg=CARD,
            fg=AMBER,
        ).pack(side="right")

        self.pending_body = tk.Frame(pending_inner, bg=CARD)
        self.pending_body.pack(fill="x", pady=(8, 0))
        self.pending_empty = tk.Label(
            self.pending_body,
            text="暂无待处理组 · 对比窗点 X 会挂起到这里",
            font=FONT_SMALL,
            bg="#f9fbfe",
            fg=MUTED,
            padx=12,
            pady=20,
            anchor="w",
        )
        self.pending_tree_frame = tk.Frame(self.pending_body, bg=CARD)
        self.pending_tree = ttk.Treeview(
            self.pending_tree_frame,
            columns=("index", "kind", "count", "summary", "status"),
            show="headings",
            selectmode="extended",
            height=5,
        )
        for column, text, width, anchor in (
            ("index", "组号", 70, "center"),
            ("kind", "类型", 90, "center"),
            ("count", "张数", 60, "center"),
            ("summary", "概要", 300, "w"),
            ("status", "状态", 80, "center"),
        ):
            self.pending_tree.heading(column, text=text, anchor=anchor)
            self.pending_tree.column(
                column, width=width, anchor=anchor, stretch=(column == "summary")
            )
        pending_scroll = tk.Scrollbar(
            self.pending_tree_frame, orient="vertical", command=self.pending_tree.yview
        )
        self.pending_tree.configure(yscrollcommand=pending_scroll.set)
        self.pending_tree.pack(side="left", fill="x", expand=True)
        pending_scroll.pack(side="right", fill="y")
        pending_scroll.configure(
            bg="#cdd4e4",
            troughcolor=CARD,
            activebackground="#aeb8cc",
            relief="flat",
            bd=0,
            width=12,
        )
        self.pending_tree.bind("<<TreeviewSelect>>", lambda _event: self._refresh_pending_ui())
        self.pending_tree.bind("<Double-1>", self._on_pending_double_click)

        pending_footer = tk.Frame(pending_inner, bg=CARD)
        pending_footer.pack(fill="x", pady=(8, 0))
        tk.Label(
            pending_footer,
            text="选中一行后可继续处理或放弃；双击一行直接继续。",
            font=FONT_SMALL,
            bg=CARD,
            fg=MUTED,
            anchor="w",
        ).pack(side="left")
        self.btn_pending_abandon = FlatButton(
            pending_footer, "放弃处理", self._abandon_pending
        )
        self.btn_pending_abandon.pack(side="right")
        self.btn_pending_continue = FlatButton(
            pending_footer, "继续处理", self._continue_pending, primary=True
        )
        self.btn_pending_continue.pack(side="right", padx=(0, 10))
        self.btn_pending_continue.set_enabled(False)
        self.btn_pending_abandon.set_enabled(False)

        tk.Label(
            outer,
            text=f"提示：扫描只处理 .jpg / .jpeg / .png；只有逐组确认后才会移动，且保留原相对路径、重名自动加后缀。",
            font=FONT_SMALL,
            bg=BG,
            fg=MUTED,
            anchor="w",
        ).pack(fill="x", pady=(8, 0))

        self._refresh_pending_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------- 交互 ----------
    def _choose_folder(self) -> None:
        selected = filedialog.askdirectory(
            title="选择要扫描的文件夹",
            initialdir=str(self._folder or Path.home()),
            parent=self.root,
        )
        if selected:
            self._folder = Path(selected)
            self.path_var.set(str(self._folder))
            self._append_path_line(self._folder)
            if self._pending_groups:
                self.status_var.set(
                    f"还有 {len(self._pending_groups)} 组待处理 · 请先处理或放弃"
                )
                self.status_label.configure(fg=AMBER)
            else:
                self.status_var.set("就绪 · 点击“开始扫描”")
                self.status_label.configure(fg=MUTED)

    def _append_path_line(self, path: Path) -> None:
        """在日志中追加可点击复制的目录行；每行用独立 tag，互不干扰。"""
        if self._closed:
            return
        self._path_tag_seq += 1
        tag = f"path_link_{self._path_tag_seq}"
        self.log_text.configure(state="normal")
        self.log_text.insert(
            "end",
            f"当前扫描目录：{path}（左键点击复制）\n",
            ("info", "path_link", tag),
        )
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.log_text.tag_bind(
            tag, "<Button-1>", lambda _event, target=path: self._copy_path(target)
        )

    def _copy_path(self, path: Path) -> None:
        if self._closed:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(str(path))
        except tk.TclError:
            return
        self._append("✅ 路径已复制到剪贴板", "copy_ok")

    @staticmethod
    def _clamp_threshold(raw: float | str) -> int:
        """把 Scale 的浮点字符串/数值取整并钳位到 1–20。"""
        try:
            number = float(raw)
        except (TypeError, ValueError):
            number = float(DEFAULT_DISTANCE)
        return max(1, min(20, int(round(number))))

    def _apply_threshold(self, value: int) -> None:
        value = max(1, min(20, int(value)))
        self.threshold_value = value
        self.threshold_text.set(f"当前: {value}")
        if abs(float(self.threshold_var.get()) - float(value)) > 1e-9:
            self.threshold_var.set(float(value))

    def _on_threshold_change(self, raw: str) -> None:
        # ttk.Scale 在拖动过程中持续回调，这里立即同步数字与内部整数。
        self._apply_threshold(self._clamp_threshold(raw))

    def _read_distance(self) -> int:
        # 扫描前再按当前滑块位置读一次，避免只依赖显示文本。
        value = self._clamp_threshold(self.threshold_var.get())
        self._apply_threshold(value)
        return int(value)

    def _start_scan(self) -> None:
        if self._scanning or self._pending_busy:
            return
        if self._pending_groups:
            messagebox.showinfo(
                "还有待处理组",
                f"当前还有 {len(self._pending_groups)} 组被 X 挂起。\n"
                "请先在“待处理重复组”面板继续处理或放弃处理，再重新扫描。",
                parent=self.root,
            )
            return
        if self._folder is None:
            messagebox.showinfo("尚未选择文件夹", "请先选择要扫描的文件夹。", parent=self.root)
            return
        if not self._folder.is_dir():
            messagebox.showwarning(
                "目录不可用", f"文件夹不存在或不可访问：\n{self._folder}", parent=self.root
            )
            return
        try:
            distance = self._read_distance()
        except ValueError as exc:
            messagebox.showwarning("参数有误", str(exc), parent=self.root)
            return

        self._stop_event.clear()
        self._scanning = True
        self._pending_busy = False
        self._scan_completed = False
        self._last_report = None
        self._decision_queue = queue.Queue()
        self._active_dialog = None
        if self._toast is not None:
            self._toast.close()
            self._toast = None
        self._set_scanning_ui(True)
        self.status_var.set("正在扫描…")
        self.status_label.configure(fg=MUTED)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        # 新一轮日志清空后，仍保留当前目录的可点击复制行。
        self._append_path_line(self._folder)

        worker = threading.Thread(
            target=self._worker_run,
            args=(self._folder, distance, self._decision_queue),
            daemon=True,
        )
        worker.start()
        self._poll_id = self.root.after(80, self._poll_queue)

    def _worker_run(self, folder: Path, distance: int, decisions: "queue.Queue") -> None:
        def log(message: str, kind: str = "info") -> None:
            self._queue.put(("log", message, kind))

        try:
            report = scan_folder(
                folder,
                distance=distance,
                log=log,
                should_stop=self._stop_event.is_set,
            )
            groups = pending_groups(report)

            if report.cancelled or self._stop_event.is_set():
                report.cancelled = True
                log("扫描已停止，未处理任何待确认分组。", "warn")
            else:
                for index, group in enumerate(groups, 1):
                    if self._stop_event.is_set():
                        report.cancelled = True
                        log("已停止：剩余待确认分组保持原样。", "warn")
                        break

                    kind_text = "精确重复" if group.kind == "exact" else "视觉相似"
                    log(f"[待确认 {index}/{len(groups)}] {kind_text} · {group.total} 张", "group")
                    self._queue.put(
                        ("review", group, index, len(groups), report.root)
                    )
                    action, keeper = decisions.get()
                    if keeper is None:
                        keeper = pick_default_keeper(group)

                    if action == "confirm":
                        if not keeper.path.is_file():
                            log(
                                f"[缺失] {MISSING_KEEPER_ERROR}，本组未移动任何文件，"
                                f"已重新加入待处理：{keeper.rel_path}",
                                "error",
                            )
                            report.suspended_count += 1
                            self._queue.put(
                                ("suspend_add", group, index, len(groups), report.root)
                            )
                            continue
                        results = apply_review_decision(
                            report.root, group, keeper, True, log=log
                        )
                        report.moves.extend(results)
                        if any(
                            not result.ok and result.error == MISSING_KEEPER_ERROR
                            for result in results
                        ):
                            report.suspended_count += 1
                            self._queue.put(
                                ("suspend_add", group, index, len(groups), report.root)
                            )
                    elif action == "skip":
                        apply_review_decision(report.root, group, keeper, False, log=log)
                        report.skipped_groups += 1
                    else:
                        report.suspended_count += 1
                        paths = "、".join(str(item.rel_path) for item in group_members(group))
                        log(
                            f"[挂起] 第 {index}/{len(groups)} 组已保留在“待处理重复组”，"
                            f"未移动任何文件：{paths}",
                            "warn",
                        )

                if self._stop_event.is_set() and not report.cancelled:
                    report.cancelled = True
                    log("已停止：剩余待确认分组保持原样。", "warn")

            log(report.summary_text(), "summary")
            self._queue.put(("done", report))
        except Exception as exc:  # 把异常交回 UI 线程显示
            self._queue.put(("error", str(exc)))

    def _poll_queue(self) -> None:
        if self._closed:
            return
        try:
            while not self._closed:
                item = self._queue.get_nowait()
                kind = item[0]
                if kind == "log":
                    self._append(item[1], item[2])
                elif kind == "review":
                    self._show_review(item[1], item[2], item[3], item[4])
                elif kind == "suspend_add":
                    self._add_suspended(item[1], item[2], item[3], item[4])
                elif kind == "done":
                    self._finish(item[1])
                elif kind == "error":
                    self._fail(item[1])
        except queue.Empty:
            pass

        if self._scanning and not self._closed:
            self._poll_id = self.root.after(80, self._poll_queue)

    def _show_review(self, group, index: int, total: int, root: Path) -> None:
        """主线程创建并等待确认弹窗；结果写回工作线程的队列。"""
        if self._closed:
            return
        if self._stop_event.is_set():
            self._decision_queue.put(("skip", None))
            self._append("已停止：跳过尚未弹出的确认组，未移动文件。", "warn")
            return
        self.status_var.set(f"等待确认 · 第 {index}/{total} 组")
        self.status_label.configure(fg=ACCENT)
        dialog = ReviewDialog(self.root, group, index, total)
        self._active_dialog = dialog
        try:
            action, keeper = dialog.show()
        finally:
            self._active_dialog = None
        if self._closed:
            return
        if action == "suspend":
            self._add_suspended(group, index, total, root)
        self._decision_queue.put((action, keeper))
        self.status_var.set("继续扫描…")
        self.status_label.configure(fg=MUTED)

    # ---------- 待处理重复组面板 ----------
    def _add_suspended(self, group, index: int, total: int, root: Path) -> None:
        if self._closed:
            return
        self._pending_uid += 1
        suspended = SuspendedGroup(
            group=group, index=index, total=total, root=Path(root), uid=self._pending_uid
        )
        self._pending_groups.append(suspended)
        first = group_members(group)[0]
        parent = str(first.rel_path.parent)
        if parent in ("", "."):
            parent = "根目录"
        summary = f"{first.path.name} · {parent}"
        kind_text = "完全重复" if group.kind == "exact" else "相似"
        self.pending_tree.insert(
            "",
            "end",
            iid=str(suspended.uid),
            values=(f"{index}/{total}", kind_text, group.total, summary, "待处理"),
        )
        self._refresh_pending_ui()

    def _refresh_pending_ui(self) -> None:
        has = bool(self._pending_groups)
        busy = self._scanning or self._pending_busy or self._closed
        if has and not self.pending_tree.selection():
            # 有新挂起组时默认选中第一行，让“继续处理”立即可用。
            children = self.pending_tree.get_children()
            if children:
                self.pending_tree.selection_set(children[0])
                self.pending_tree.focus(children[0])
        selected = bool(self.pending_tree.selection()) if has else False
        self.btn_start.set_enabled(not busy and not has)
        self.btn_choose_folder.set_enabled(not busy)
        self.btn_pending_continue.set_enabled(not busy and has and selected)
        self.btn_pending_abandon.set_enabled(not busy and has and selected)
        self.pending_count_var.set(f"共 {len(self._pending_groups)} 组" if has else "")
        if has:
            self.pending_empty.pack_forget()
            if not self.pending_tree_frame.winfo_manager():
                self.pending_tree_frame.pack(fill="x")
        else:
            self.pending_tree_frame.pack_forget()
            if not self.pending_empty.winfo_manager():
                self.pending_empty.pack(fill="x")

    def _set_pending_status(self, suspended: SuspendedGroup, status: str) -> None:
        try:
            if self.pending_tree.exists(str(suspended.uid)):
                self.pending_tree.set(str(suspended.uid), "status", status)
        except tk.TclError:
            pass

    def _remove_suspended(self, suspended: SuspendedGroup) -> None:
        try:
            self.pending_tree.delete(str(suspended.uid))
        except tk.TclError:
            pass
        if suspended in self._pending_groups:
            self._pending_groups.remove(suspended)
        self._refresh_pending_ui()

    def _on_pending_double_click(self, event) -> None:
        if self._closed or self._scanning or self._pending_busy:
            return
        iid = self.pending_tree.identify_row(event.y)
        if not iid:
            return
        self.pending_tree.selection_set(iid)
        self._refresh_pending_ui()
        self._continue_pending(only_iid=iid)

    def _continue_pending(self, only_iid: str | None = None) -> None:
        if self._closed or self._scanning or self._pending_busy:
            return
        if only_iid:
            items = [
                suspended
                for suspended in self._pending_groups
                if str(suspended.uid) == only_iid
            ]
        else:
            lookup = {str(suspended.uid): suspended for suspended in self._pending_groups}
            items = [
                lookup[iid] for iid in self.pending_tree.selection() if iid in lookup
            ]
        if not items:
            self.status_var.set("请先在待处理列表选择一组")
            self.status_label.configure(fg=MUTED)
            return

        items.sort(key=lambda suspended: suspended.index)
        self._pending_busy = True
        self._refresh_pending_ui()
        self.status_var.set(f"正在继续处理 {len(items)} 组挂起组…")
        self.status_label.configure(fg=ACCENT)
        try:
            for suspended in items:
                if self._closed:
                    break
                self._set_pending_status(suspended, "处理中")
                dialog = ReviewDialog(
                    self.root, suspended.group, suspended.index, suspended.total
                )
                self._active_dialog = dialog
                try:
                    action, keeper = dialog.show()
                finally:
                    self._active_dialog = None
                if self._closed:
                    break

                if action == "suspend":
                    self._set_pending_status(suspended, "待处理")
                    self._append(
                        f"[挂起] 第 {suspended.index}/{suspended.total} 组仍在待处理列表，"
                        "未移动任何文件。",
                        "warn",
                    )
                elif action == "skip":
                    apply_review_decision(
                        suspended.root, suspended.group, keeper, False, log=self._append
                    )
                    self._remove_suspended(suspended)
                else:
                    apply_review_decision(
                        suspended.root, suspended.group, keeper, True, log=self._append
                    )
                    if keeper.path.is_file():
                        self._remove_suspended(suspended)
                    else:
                        self._set_pending_status(suspended, "待处理")
                        self._append(
                            f"[缺失] 第 {suspended.index}/{suspended.total} 组保留项不存在，"
                            "未移动，仍留在待处理列表。",
                            "error",
                        )
                self._refresh_pending_ui()
        finally:
            self._pending_busy = False
            self._refresh_pending_ui()

        if self._closed:
            return
        if self._pending_groups:
            self.status_var.set(
                f"还有 {len(self._pending_groups)} 组待处理 · 可继续处理或放弃"
            )
            self.status_label.configure(fg=AMBER)
        else:
            self._pending_cleared()

    def _abandon_pending(self) -> None:
        if self._closed or self._scanning or self._pending_busy:
            return
        lookup = {str(suspended.uid): suspended for suspended in self._pending_groups}
        items = [
            lookup[iid] for iid in self.pending_tree.selection() if iid in lookup
        ]
        if not items:
            self.status_var.set("请先在待处理列表选择一组")
            self.status_label.configure(fg=MUTED)
            return

        for suspended in items:
            paths = "、".join(
                str(item.rel_path) for item in group_members(suspended.group)
            )
            self._append(
                f"[放弃处理] 用户主动放弃第 {suspended.index}/{suspended.total} 组，"
                f"未移动任何文件：{paths}",
                "warn",
            )
            self._remove_suspended(suspended)

        if self._pending_groups:
            self.status_var.set(
                f"还有 {len(self._pending_groups)} 组待处理 · 可继续处理或放弃"
            )
            self.status_label.configure(fg=AMBER)
        else:
            self._pending_cleared()

    def _pending_cleared(self) -> None:
        report = self._last_report
        status = "扫描完成 · 待处理组已全部处理"
        if report is not None:
            status += f" · 移入“{TRASH_DIR_NAME}” {report.moved_count} 个"
            if report.skipped_images:
                status += f" · 跳过 {report.skipped_images} 张无法读取"
        self.status_var.set(status + " · 可重新扫描")
        self.status_label.configure(fg=GREEN)
        self._append("待处理重复组已全部处理完，可重新扫描。", "summary")
        self._refresh_pending_ui()
        if self._scan_completed:
            self._show_toast("扫描完成！")

    def _stop_scan(self) -> None:
        if not self._scanning:
            return
        self._stop_event.set()
        self.btn_stop.set_enabled(False)
        self.status_var.set("正在停止…")
        self._append(
            "已请求停止：不再弹出新的确认组；当前已弹出的组仍按你的选择处理。",
            "warn",
        )

    def _finish(self, report: ScanReport) -> None:
        self._scanning = False
        self._poll_id = None
        self._last_report = report
        self._scan_completed = not report.cancelled
        self._set_scanning_ui(False)
        pending = len(self._pending_groups)

        if report.cancelled:
            status = f"已停止 · 已移入“{TRASH_DIR_NAME}” {report.moved_count} 个"
            if report.skipped_images:
                status += f" · 跳过 {report.skipped_images} 张无法读取"
            if pending:
                status += f" · 还有 {pending} 组待处理"
                self._append(
                    f"扫描已停止：还有 {pending} 组已挂起，"
                    "可在“待处理重复组”继续处理或放弃。",
                    "warn",
                )
            self.status_var.set(status)
            self.status_label.configure(fg=AMBER)
            self._refresh_pending_ui()
            return

        status = f"扫描完成 · 移入“{TRASH_DIR_NAME}” {report.moved_count} 个"
        if report.skipped_groups:
            status += f" · 跳过 {report.skipped_groups} 组"
        if report.skipped_images:
            status += f" · 跳过 {report.skipped_images} 张无法读取"
        if report.failed_moves:
            status += f" · 失败 {len(report.failed_moves)} 个"
        if pending:
            status += f" · 还有 {pending} 组待处理"
        self.status_var.set(status)
        has_issue = bool(report.failed_moves or pending or report.skipped_images)
        self.status_label.configure(fg=AMBER if has_issue else GREEN)
        if pending:
            self._append(
                f"扫描完成：还有 {pending} 组已挂起，"
                "可在“待处理重复组”继续处理或放弃。",
                "summary",
            )
            self._show_toast(f"扫描完成，还有 {pending} 组待处理")
        elif report.skipped_images:
            self._show_toast(f"扫描完成，跳过 {report.skipped_images} 张无法读取")
        else:
            self._show_toast("扫描完成！")
        self._refresh_pending_ui()

    def _show_toast(self, text: str) -> None:
        if self._closed:
            return
        if self._toast is not None:
            self._toast.close()
        self._toast = Toast(self.root, text)

    def _fail(self, message: str) -> None:
        self._scanning = False
        self._poll_id = None
        self._scan_completed = False
        self._set_scanning_ui(False)
        self.status_var.set("扫描失败 · 详情见日志")
        self.status_label.configure(fg=RED)
        self._append(f"扫描失败：{message}", "error")
        self._refresh_pending_ui()

    def _set_scanning_ui(self, scanning: bool) -> None:
        self.btn_stop.set_enabled(scanning)
        self.threshold_scale.state(["disabled"] if scanning else ["!disabled"])
        if scanning:
            self.progress.pack(side="right", pady=(3, 0))
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
        self._refresh_pending_ui()

    def _append(self, message: str, kind: str = "info") -> None:
        if self._closed:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n", kind)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _on_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._toast is not None:
            self._toast.close()
            self._toast = None
        self._stop_event.set()
        dialog = self._active_dialog
        self._active_dialog = None
        if dialog is not None:
            try:
                dialog.cancel()
            except tk.TclError:
                pass
        # 唤醒可能正在等待确认结果的工作线程，让它以“跳过”处理后退出。
        try:
            self._decision_queue.put(("skip", None))
        except Exception:
            pass
        if self._poll_id is not None:
            try:
                self.root.after_cancel(self._poll_id)
            except tk.TclError:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main() -> None:
    # 必须早于任何 tk.Tk()：开启 DPI 感知后再创建窗口，2K/4K 下才不会发虚。
    _enable_windows_dpi_awareness()
    root = tk.Tk()
    configure_dpi_scaling(root)
    PixFilterApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
