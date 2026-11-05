"""人工确认所需的 Tk 组件：主题、扁平按钮、并排对比弹窗、大图窗口。

所有代码都必须在主线程调用；本模块不启动工作线程。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from typing import Optional

from PIL import Image, ImageTk

from pixfilter_core import (
    TRASH_DIR_NAME,
    DuplicateGroup,
    ImageInfo,
    format_size,
    group_members,
    pick_default_keeper,
)

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
THUMB_BG = "#eef2fa"

FONT_UI = ("Microsoft YaHei UI", 10)
FONT_BOLD = ("Microsoft YaHei UI", 10, "bold")
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 14, "bold")
FONT_LOG = ("Microsoft YaHei UI", 9)


THUMB_MAX = 300


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


class ImageViewer:
    """高清大图窗口：等比缩放到屏幕 80%，关闭时显式释放图像引用。"""

    def __init__(self, parent: tk.Misc, info: ImageInfo):
        self.window = tk.Toplevel(parent)
        self.window.title(f"查看大图 · {info.path.name}")
        self.window.configure(bg="#11141c")
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Escape>", lambda _event: self.close())
        self.window.bind("<space>", lambda _event: self.close())

        self._photo: Optional[ImageTk.PhotoImage] = None
        self._image: Optional[Image.Image] = None
        self.label: Optional[tk.Label] = None
        self._closed = False

        max_width = max(320, int(self.window.winfo_screenwidth() * 0.8))
        max_height = max(240, int(self.window.winfo_screenheight() * 0.8))
        try:
            with Image.open(info.path) as source:
                image = source.convert("RGB")
            # 必须用 thumbnail 等比缩放，绝不把原图直接塞进 Label。
            image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            tk.Label(
                self.window,
                text=f"无法加载图片：\n{info.path}",
                font=FONT_UI,
                bg="#11141c",
                fg="#f0f0f0",
            ).pack(padx=24, pady=24)
        else:
            self._image = image
            self._photo = photo
            self.label = tk.Label(self.window, image=photo, bg="#11141c", bd=0)
            self.label.image = photo  # 显式保存引用，避免白板
            self.label.pack()

        tk.Label(
            self.window,
            text=f"{info.rel_path} · {info.width}×{info.height} · {format_size(info.file_size)}",
            font=FONT_SMALL,
            bg="#11141c",
            fg="#9aa3ba",
        ).pack(pady=(8, 10))

        self.window.update_idletasks()
        width = self.window.winfo_reqwidth()
        height = self.window.winfo_reqheight()
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.window.geometry(f"{width}x{height}+{x}+{y}")
        self.window.focus_force()
        self.window.focus_set()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.label is not None:
            try:
                self.label.configure(image="")
            except tk.TclError:
                pass
            self.label.image = None
            self.label = None
        self._photo = None
        if self._image is not None:
            self._image.close()
            self._image = None
        try:
            self.window.destroy()
        except tk.TclError:
            pass


class ReviewDialog:
    """一组一确认的并排对比窗口。

    show() 在主线程进入嵌套事件循环，返回 (action, keeper)。
    action 为 "confirm" / "skip" / "suspend"：
    - confirm：确认保留 keeper，移动其余项；
    - skip：本次跳过，一次性放弃；
    - suspend：点 X 或 Escape，保留在待处理面板，稍后可继续处理。
    """

    def __init__(self, parent: tk.Misc, group: DuplicateGroup, index: int, total: int):
        self.group = group
        self.members = group_members(group)
        self.default = pick_default_keeper(group)
        self.result: Optional[tuple[str, ImageInfo]] = None
        self.window = tk.Toplevel(parent)
        kind_text = "完全重复" if group.kind == "exact" else "视觉相似"
        self.window.title(f"PixFilter 确认 · 第 {index}/{total} 组 · {kind_text}")
        self.window.configure(bg=BG)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.minsize(760, 560)
        self.window.resizable(True, True)

        self._finished = False
        self._thumbs: list[ImageTk.PhotoImage] = []
        self._thumb_labels: list[tk.Label] = []
        self._status_labels: dict[str, tk.Label] = {}
        self._viewers: list[ImageViewer] = []
        self._selected = tk.StringVar(value=str(self.default.path))

        self._build(index, total, kind_text)
        self._center(parent)
        self.window.focus_force()

    def _build(self, index: int, total: int, kind_text: str) -> None:
        outer = tk.Frame(self.window, bg=BG, padx=22, pady=16)
        outer.pack(fill="both", expand=True)

        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        tk.Label(
            header,
            text=f"第 {index}/{total} 组 · {kind_text} · {len(self.members)} 张",
            font=FONT_TITLE,
            bg=BG,
            fg=TEXT,
            anchor="w",
        ).pack(side="left")
        detail = ""
        if self.group.kind == "similar":
            detail = f"最大 pHash 差异 {self.group.max_distance}"
        tk.Label(
            header,
            text=detail,
            font=FONT_SMALL,
            bg=BG,
            fg=MUTED,
        ).pack(side="right", pady=(6, 0))

        tk.Label(
            outer,
            text=f"请选择要保留的一张，其余会在确认后移入“{TRASH_DIR_NAME}”。"
            "点击缩略图可查看大图。",
            font=FONT_SMALL,
            bg=BG,
            fg=MUTED,
            anchor="w",
        ).pack(fill="x", pady=(4, 10))

        body = tk.Frame(outer, bg=BG)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0, bd=0)
        self.hbar = tk.Scrollbar(
            body,
            orient="horizontal",
            command=self.canvas.xview,
            relief="flat",
            bd=0,
            width=12,
            bg="#cdd4e4",
            troughcolor=BG,
            activebackground="#aeb8cc",
        )
        self.canvas.configure(xscrollcommand=self.hbar.set)
        self.canvas.pack(side="top", fill="both", expand=True)
        self.hbar.pack(side="bottom", fill="x")

        self.inner = tk.Frame(self.canvas, bg=BG)
        self._inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._sync_scrollregion)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel, add="+")
        self.canvas.bind("<Shift-MouseWheel>", self._on_mousewheel, add="+")
        self.canvas.bind("<Button-4>", lambda _event: self._scroll_units(-3), add="+")
        self.canvas.bind("<Button-5>", lambda _event: self._scroll_units(3), add="+")

        for info in self.members:
            self._build_card(self.inner, info)
        self._bind_wheel_tree(self.inner)
        self._refresh_status()

        footer = tk.Frame(outer, bg=BG)
        footer.pack(fill="x", pady=(12, 0))
        tk.Label(
            footer,
            text="点“跳过”= 本次放弃该组；关闭窗口（X）= 挂起，可稍后在主界面继续处理。两种都不会移动文件。",
            font=FONT_SMALL,
            bg=BG,
            fg=MUTED,
            anchor="w",
            wraplength=460,
        ).pack(side="left")
        self.btn_confirm = FlatButton(
            footer,
            "保留选中的，移动其他图片",
            lambda: self._finish("confirm"),
            primary=True,
        )
        self.btn_confirm.pack(side="right")
        self.btn_skip = FlatButton(footer, "跳过", lambda: self._finish("skip"))
        self.btn_skip.pack(side="right", padx=(0, 10))

    def _build_card(self, parent: tk.Misc, info: ImageInfo) -> None:
        card = tk.Frame(
            parent,
            bg=CARD,
            width=326,
            height=478,
            highlightbackground=BORDER,
            highlightthickness=1,
            bd=0,
        )
        card.pack(side="left", padx=8, pady=6)
        card.pack_propagate(False)

        content = tk.Frame(card, bg=CARD)
        content.pack(fill="both", expand=True, padx=12, pady=12)

        thumb_box = tk.Frame(content, bg=THUMB_BG, width=THUMB_MAX, height=THUMB_MAX)
        thumb_box.pack()
        thumb_box.pack_propagate(False)

        try:
            with Image.open(info.path) as source:
                image = source.convert("RGB")
            # thumbnail 严格保持比例，最大 300×300。
            image.thumbnail((THUMB_MAX, THUMB_MAX), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(image)
        except Exception:
            tk.Label(
                thumb_box,
                text="无法预览",
                font=FONT_SMALL,
                bg=THUMB_BG,
                fg=MUTED,
            ).place(relx=0.5, rely=0.5, anchor="center")
        else:
            label = tk.Label(thumb_box, image=photo, bg=THUMB_BG, bd=0, cursor="hand2")
            label.image = photo
            label.place(relx=0.5, rely=0.5, anchor="center")
            self._thumbs.append(photo)
            self._thumb_labels.append(label)
            label.bind("<Button-1>", lambda _event, item=info: self._open_viewer(item))

        thumb_box.bind("<Button-1>", lambda _event, item=info: self._open_viewer(item))
        thumb_box.configure(cursor="hand2")

        tk.Label(
            content,
            text=info.path.name,
            font=FONT_BOLD,
            bg=CARD,
            fg=TEXT,
            anchor="w",
            justify="left",
            wraplength=THUMB_MAX,
        ).pack(fill="x", pady=(10, 2))
        directory = str(info.rel_path.parent)
        if directory not in ("", "."):
            tk.Label(
                content,
                text=directory,
                font=FONT_SMALL,
                bg=CARD,
                fg=MUTED,
                anchor="w",
                justify="left",
                wraplength=THUMB_MAX,
            ).pack(fill="x")
        resolution = f"{info.width}×{info.height}" if info.width and info.height else "尺寸未知"
        tk.Label(
            content,
            text=f"{resolution} · {format_size(info.file_size)}",
            font=FONT_SMALL,
            bg=CARD,
            fg=MUTED,
            anchor="w",
        ).pack(fill="x", pady=(2, 6))

        tk.Radiobutton(
            content,
            text="保留这一张",
            variable=self._selected,
            value=str(info.path),
            command=self._refresh_status,
            font=FONT_UI,
            bg=CARD,
            fg=TEXT,
            activebackground=CARD,
            activeforeground=TEXT,
            selectcolor=CARD,
            highlightthickness=0,
            anchor="w",
            cursor="hand2",
        ).pack(fill="x")

        status = tk.Label(content, font=FONT_SMALL, bg=CARD, anchor="w")
        status.pack(fill="x", pady=(2, 0))
        self._status_labels[str(info.path)] = status

    def _refresh_status(self) -> None:
        selected = self._selected.get()
        for path, label in self._status_labels.items():
            if path == selected:
                label.configure(text="✓ 当前将被保留", fg=GREEN)
            else:
                label.configure(text=f"→ 确认后移入“{TRASH_DIR_NAME}”", fg=AMBER)

    def _sync_scrollregion(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _scroll_units(self, units: int) -> None:
        self.canvas.xview_scroll(units, "units")

    def _on_mousewheel(self, event) -> str:
        number = getattr(event, "num", 0)
        if number == 4:
            self._scroll_units(-3)
        elif number == 5:
            self._scroll_units(3)
        else:
            delta = int(getattr(event, "delta", 0) or 0)
            if delta:
                self._scroll_units(-3 if delta > 0 else 3)
        return "break"

    def _bind_wheel_tree(self, widget: tk.Misc) -> None:
        widget.bind("<MouseWheel>", self._on_mousewheel, add="+")
        widget.bind("<Shift-MouseWheel>", self._on_mousewheel, add="+")
        widget.bind("<Button-4>", lambda _event: self._scroll_units(-3), add="+")
        widget.bind("<Button-5>", lambda _event: self._scroll_units(3), add="+")
        for child in widget.winfo_children():
            self._bind_wheel_tree(child)

    def _open_viewer(self, info: ImageInfo) -> None:
        viewer = ImageViewer(self.window, info)
        self._viewers.append(viewer)

    def _center(self, parent: tk.Misc) -> None:
        self.window.update_idletasks()
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        desired_width = len(self.members) * 342 + 70
        width = min(int(screen_w * 0.92), max(780, desired_width))
        height = min(int(screen_h * 0.9), 700)
        try:
            parent_x = parent.winfo_rootx()
            parent_y = parent.winfo_rooty()
            parent_w = parent.winfo_width()
            parent_h = parent.winfo_height()
        except tk.TclError:
            parent_x, parent_y, parent_w, parent_h = 0, 0, screen_w, screen_h
        x = min(max(0, parent_x + (parent_w - width) // 2), max(0, screen_w - width))
        y = min(max(0, parent_y + (parent_h - height) // 2), max(0, screen_h - height))
        self.window.geometry(f"{width}x{height}+{x}+{y}")

    def show(self) -> tuple[str, ImageInfo]:
        try:
            self.window.wait_window()
        except tk.TclError:
            pass
        return self.result or ("suspend", self.default)

    def _finish(self, action: str) -> None:
        if self._finished:
            return
        self._finished = True
        selected = next(
            (item for item in self.members if str(item.path) == self._selected.get()),
            self.default,
        )
        self.result = (action, selected)
        for viewer in self._viewers:
            viewer.close()
        self._viewers.clear()
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def cancel(self) -> None:
        """X / Escape：挂起，而不是丢弃或跳过。"""
        self._finish("suspend")


def compute_toast_width(text_width: float, root_width: int, min_width: int = 200) -> int:
    """纯几何计算：按文字宽度加宽，下限 200，上限主窗口宽 90% 且绝不溢出。"""
    root_width = max(1, int(root_width))
    cap = max(1, min(root_width, int(root_width * 0.9)))
    wanted = max(int(min_width), int(round(text_width)) + 44)
    return int(min(wanted, cap))


def compute_toast_position(
    root_x: int, root_y: int, root_width: int, toast_width: int,
    toast_height: int, top_pad: int = 10,
) -> tuple[int, int, int]:
    """纯几何计算：返回 (水平坐标, 停靠 y, 滑入起点 y)。

    水平方向在 [root_x, root_x + root_width] 内居中且不溢出；
    停靠点为主窗口顶部内边缘，起点为停靠点上方一个自身高度。
    """
    root_width = max(1, int(root_width))
    width = min(max(1, int(toast_width)), root_width)
    x = int(root_x) + max(0, (root_width - width) // 2)
    target_y = int(root_y) + int(top_pad)
    start_y = target_y - int(toast_height)
    return x, target_y, start_y


class Toast:
    """从主窗口顶边滑出的绿色 pill 提示条。

    位置每次显示时按主窗口当时的全局坐标重新计算；由 root.after 递归驱动
    滑入约 120ms → 停留 1500ms → 原地淡出约 400ms，全程不阻塞主线程。
    """

    STEP_MS = 15
    SLIDE_MS = 120
    HOLD_MS = 1500
    FADE_STEP = 0.1
    FADE_INTERVAL_MS = 40
    TOP_PAD = 10
    HEIGHT = 40
    MIN_WIDTH = 200

    def __init__(self, root: tk.Misc, text: str):
        self.root = root
        self.text = text
        self._closed = False
        self._after_ids: set[str] = set()

        key = BG
        self.window = tk.Toplevel(root)
        # 先隐藏，避免 Toplevel 默认在 (0,0) 闪现；坐标算好后再显示。
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg=key)
        try:
            self.window.attributes("-alpha", 1.0)
        except tk.TclError:
            pass
        try:
            self.window.attributes("-transparentcolor", key)
        except tk.TclError:
            pass

        self._font = tkfont.Font(root=self.window, family="Microsoft YaHei", size=11)
        self._key = key
        self._canvas: tk.Canvas | None = None
        self._place_tries = 0
        self._placed = False
        self._place_and_show()

    def _place_and_show(self) -> None:
        if self._closed or self._placed:
            return
        # 主窗口可能刚被移动/最大化，先结算几何再取当时坐标。
        try:
            self.root.update_idletasks()
        except tk.TclError:
            pass
        root_w = int(self.root.winfo_width())
        if root_w <= 1 or not bool(self.root.winfo_ismapped()):
            # 窗口尚未映射时 winfo_rootx/rooty 不可信，稍后重试而不是落到 (0,0)。
            if self._place_tries < 4:
                self._place_tries += 1
                self._schedule(self._place_and_show, 30)
            else:
                self.close()
            return

        root_x = int(self.root.winfo_rootx())
        root_y = int(self.root.winfo_rooty())
        height = Toast.HEIGHT
        width = compute_toast_width(self._font.measure(self.text), root_w, Toast.MIN_WIDTH)
        x, target_y, start_y = compute_toast_position(
            root_x, root_y, root_w, width, height, Toast.TOP_PAD
        )
        self._width = width
        self._height = height
        self._x, self._target_y, self._start_y = x, target_y, start_y
        self._steps = max(1, Toast.SLIDE_MS // Toast.STEP_MS)

        self._canvas = tk.Canvas(
            self.window, width=width, height=height, bg=self._key,
            highlightthickness=0, bd=0,
        )
        self._canvas.pack()
        self._rounded_rect(
            self._canvas, 1, 1, width - 1, height - 1, height // 2 - 1,
            fill="#4CAF50", outline="#43A047",
        )
        self._canvas.create_text(
            width / 2, height / 2, text=self.text, fill="#ffffff", font=self._font
        )

        self._move(self._start_y)
        try:
            self.window.attributes("-topmost", True)
            self.window.deiconify()
            self.window.update_idletasks()
        except tk.TclError:
            self.close()
            return
        self._placed = True
        self._schedule(lambda: self._slide_in(0), Toast.STEP_MS)

    @property
    def canvas(self) -> tk.Canvas | None:
        return self._canvas

    @staticmethod
    def _rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, radius, **kwargs):
        points = (
            x1 + radius, y1,
            x2 - radius, y1,
            x2, y1,
            x2, y1 + radius,
            x2, y2 - radius,
            x2, y2,
            x2 - radius, y2,
            x1 + radius, y2,
            x1, y2,
            x1, y2 - radius,
            x1, y1 + radius,
            x1, y1,
        )
        return canvas.create_polygon(points, smooth=True, **kwargs)

    def _move(self, y: float) -> None:
        try:
            self.window.geometry(
                f"{self._width}x{self._height}+{self._x}+{int(round(y))}"
            )
        except tk.TclError:
            self.close()

    def _schedule(self, callback, delay: int) -> None:
        if self._closed:
            return

        def run() -> None:
            self._after_ids.discard(aid)
            if self._closed:
                return
            try:
                callback()
            except tk.TclError:
                self.close()

        try:
            aid = self.root.after(delay, run)
        except tk.TclError:
            return
        self._after_ids.add(aid)

    def _slide_in(self, frame: int) -> None:
        frame += 1
        span = self._target_y - self._start_y
        if frame >= self._steps:
            self._move(self._target_y)
            self._schedule(lambda: self._fade_out(1.0), Toast.HOLD_MS)
            return
        progress = 1 - (1 - frame / self._steps) ** 3
        self._move(self._start_y + span * progress)
        self._schedule(lambda: self._slide_in(frame), Toast.STEP_MS)

    def _fade_out(self, alpha: float = 1.0) -> None:
        if self._closed:
            return
        value = round(max(0.0, min(1.0, alpha)), 2)
        try:
            self.window.attributes("-alpha", value)
        except tk.TclError:
            # 平台不支持透明属性时直接销毁，不抛错、不阻塞。
            self.close()
            return
        if value <= 0.0:
            self.close()
            return
        next_value = round(value - Toast.FADE_STEP, 2)
        self._schedule(lambda: self._fade_out(next_value), Toast.FADE_INTERVAL_MS)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for aid in list(self._after_ids):
            try:
                self.root.after_cancel(aid)
            except tk.TclError:
                pass
        self._after_ids.clear()
        try:
            if self.window.winfo_exists():
                self.window.destroy()
        except tk.TclError:
            pass

    def is_alive(self) -> bool:
        if self._closed:
            return False
        try:
            return bool(self.window.winfo_exists())
        except tk.TclError:
            return False

