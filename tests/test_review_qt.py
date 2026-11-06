"""Qt 对比弹窗（PySide6）的外层测试。

隔离方式：所有 Qt 场景都在 ``tests/qt_review_cases.py`` 的独立子进程中执行，
主 unittest 进程不导入 PySide6 / 不创建 QApplication，平台固定为 offscreen。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES = PROJECT_ROOT / "tests" / "qt_review_cases.py"
FATAL_MARKERS = (
    "QApplication was not created in the main thread",
    "main thread is not in main loop",
    "Tcl_AsyncDelete",
    "Segmentation fault",
    "Fatal Python error",
)


class QtReviewSubprocessTest(unittest.TestCase):
    def _run(self, case: str, *, timeout: float = 25.0):
        env = os.environ.copy()
        env.pop("QT_QPA_PLATFORM", None)
        # 固定 offscreen：不存在“真机”分支，避免测试抢桌面。
        env["QT_QPA_PLATFORM"] = "offscreen"
        command = [sys.executable, str(CASES), case]
        process = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        self.assertEqual(
            process.returncode,
            0,
            msg=f"子进程失败（{case}）\nstdout:\n{process.stdout}\nstderr:\n{process.stderr}",
        )
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"子进程没有输出 JSON：{case}")
        payload = json.loads(lines[-1])
        self.assertTrue(payload.get("ok"), payload)
        for marker in FATAL_MARKERS:
            self.assertNotIn(marker, process.stderr, f"{case} 出现致命标记：{marker}")
        return payload, process.stderr

    # ---------- a) offscreen 可用性 & 不引入旧 GUI 框架 ----------
    def test_a_offscreen_import_and_no_legacy_gui(self) -> None:
        payload, _stderr = self._run("a")
        self.assertEqual(payload["platform"], "offscreen")
        self.assertTrue(payload["qapp_ok"])
        self.assertFalse(payload["tkinter_after_import"])

    # ---------- b) 卡片数量 / 尺寸 / 默认项 / QPixmap 存活 ----------
    def test_b_cards_sizes_default_and_pixmap_survives_gc(self) -> None:
        payload, _stderr = self._run("b")
        exact = payload["exact"]
        self.assertEqual(exact["card_count"], 3)
        self.assertEqual(exact["thumbnail_count"], 3)
        self.assertEqual(exact["placeholder_count"], 0)
        self.assertEqual(exact["default_path"], "a.png")
        self.assertEqual(exact["selected_path"], "a.png")
        self.assertTrue(all(exact["max_bound"]))
        self.assertTrue(exact["aspect_ok"])
        self.assertEqual(payload["similar_default"], "big.png")
        self.assertTrue(payload["gc_still_renderable"])
        self.assertTrue(payload["grab_ok"])

    # ---------- c) 缺失图片占位 + 日志 ----------
    def test_c_missing_image_placeholder_and_log(self) -> None:
        payload, _stderr = self._run("c")
        self.assertEqual(payload["placeholder_count"], 1)
        self.assertEqual(payload["thumbnail_count"], 1)
        self.assertTrue(payload["log_has_path"])
        self.assertTrue(payload["log_has_exception"])
        self.assertTrue(payload["single_failure_keeps_other"])
        self.assertIn("FileNotFoundError", payload["placeholder_text"])

    # ---------- d) 三种 action 映射到 ScanService ----------
    def test_d_decision_actions_map_to_service(self) -> None:
        payload, _stderr = self._run("d")
        self.assertEqual(payload["confirm_action"], "confirm")
        self.assertGreaterEqual(payload["confirm_moves"], 1)
        self.assertEqual(payload["confirm_trash_files"], 1)
        self.assertGreaterEqual(payload["confirm_undo_stack"], 1)
        self.assertEqual(payload["skip_action"], "skip")
        self.assertEqual(payload["skip_moves"], 0)
        self.assertTrue(all(payload["skip_files_still_direct"]))
        self.assertEqual(payload["suspend_action"], "suspend")
        self.assertEqual(payload["suspend_moves"], 0)
        self.assertTrue(payload["suspend_flag"])

    # ---------- e) 线程语义 + 真实服务 → 信号 → 弹窗 → 决策 ----------
    def test_e_thread_guard_and_signal_chain(self) -> None:
        payload, _stderr = self._run("e")
        self.assertEqual(payload["worker_errors"].count("RuntimeError"), 2)
        self.assertEqual(payload["qthread_slot_thread"], "MainThread")
        self.assertEqual(payload["qthread_value"], "from-qthread")
        self.assertTrue(payload["service_started"])
        self.assertEqual(payload["handler_thread"], "MainThread")
        self.assertTrue(payload["finished_once"])
        self.assertEqual(payload["final_state"], "idle")
        self.assertGreaterEqual(payload["moves"], 1)
        self.assertEqual(payload["trash_files"], 1)
        self.assertGreaterEqual(payload["undo_batches"], 1)

    # ---------- f) UI 美化：居中 / 卡片质感 / 选中 / 按钮三态 / 逻辑回归 ----------
    def test_f_ui_polish(self) -> None:
        payload, _stderr = self._run("ui_polish", timeout=30.0)

        centering = payload["centering"]
        for width in ("900", "1400"):
            data = centering[width]
            self.assertEqual(len(data["card_widths"]), 2)
            self.assertLessEqual(
                data["diff"], 2,
                f"{width} 宽度下左右留白不对称：{data}",
            )
            self.assertGreater(data["left"], 0, f"{width} 下卡片组未居中：{data}")
            self.assertEqual(data["scroll_max"], 0)

        overflow = payload["overflow"]
        self.assertEqual(len(overflow["card_widths"]), 4)
        self.assertGreater(overflow["scroll_max"], 0, "超宽内容无法横向滚动")
        self.assertTrue(overflow["no_clip"], overflow)
        self.assertTrue(overflow["starts_at_margin"], overflow)
        self.assertGreaterEqual(
            overflow["container_width"], overflow["expected_width"]
        )

        card = payload["card"]
        for key in (
            "radius_ok",
            "selected_border_ok",
            "selected_bg_ok",
            "hover_ok",
            "padding_ok",
            "cursor_ok",
            "hover_attr",
            "shadow_is_effect",
            "shadow_ref_kept",
            "shadow_after_gc",
        ):
            self.assertTrue(card[key], f"卡片属性未生效：{key} -> {card}")
        self.assertGreaterEqual(card["shadow_blur"], 8)
        self.assertLessEqual(card["shadow_blur"], 12)
        self.assertEqual(card["shadow_offset"], [0, 2])

        selected = payload["selected"]
        self.assertEqual(selected["default"], "polish_a.png")
        self.assertEqual(selected["initial"], "polish_a.png")
        self.assertEqual(selected["after"], "polish_b.png")
        self.assertTrue(selected["old_style_reset"])
        self.assertFalse(selected["old_property"])
        self.assertTrue(selected["new_style_highlight"])
        self.assertTrue(selected["new_property"])
        self.assertFalse(selected["old_bold"])
        self.assertTrue(selected["new_bold"])
        self.assertEqual(selected["radio_text"], "保留这一张")
        self.assertTrue(selected["radio_indicator"])
        self.assertTrue(selected["focus_state"])

        click = payload["click"]
        self.assertEqual(click["card"], {"selected": "polish_a.png", "viewers": 0})
        self.assertEqual(click["thumbnail"]["viewers"], 1)
        self.assertTrue(payload["viewer_space_closed"])

        buttons = payload["buttons"]
        self.assertTrue(buttons["primary_radius_ok"])
        self.assertTrue(buttons["primary_distinct"])
        self.assertTrue(buttons["primary_disabled_gray"])
        self.assertTrue(buttons["secondary_radius_ok"])
        self.assertTrue(buttons["secondary_border_1px"])
        self.assertTrue(buttons["secondary_distinct"])
        self.assertTrue(buttons["disabled_click_noop"])

        placeholder = payload["placeholder"]
        self.assertEqual(placeholder["count"], 1)
        self.assertTrue(placeholder["single_failure_keeps_other"])
        self.assertTrue(placeholder["text_ok"])
        self.assertTrue(placeholder["style_ok"])
        self.assertTrue(placeholder["log_ok"])

        decisions = payload["decisions"]
        confirm = decisions["confirm"]
        self.assertEqual(confirm["action"], "confirm")
        self.assertGreaterEqual(confirm["moves"], 1)
        self.assertEqual(confirm["trash_files"], 1)
        self.assertGreaterEqual(confirm["undo_batches"], 1)
        skip = decisions["skip"]
        self.assertEqual(skip["action"], "skip")
        self.assertEqual(skip["moves"], 0)
        self.assertTrue(all(skip["files_still_direct"]))
        suspend = decisions["suspend"]
        self.assertEqual(suspend["action"], "suspend")
        self.assertEqual(suspend["moves"], 0)
        self.assertTrue(suspend["suspended"])
        self.assertTrue(all(suspend["files_still_direct"]))


if __name__ == "__main__":
    unittest.main()
