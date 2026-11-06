"""Qt 批处理复核工作台的外层测试（全部在独立子进程 + offscreen 运行）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASES = PROJECT_ROOT / "tests" / "qt_workbench_cases.py"
sys.path.insert(0, str(PROJECT_ROOT))
import config  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
FATAL_MARKERS = (
    "QApplication was not created in the main thread",
    "main thread is not in main loop",
    "Tcl_AsyncDelete",
    "Segmentation fault",
    "Fatal Python error",
)


class QtWorkbenchSubprocessTest(unittest.TestCase):
    def _run(self, case: str, timeout: float = 30.0):
        env = os.environ.copy()
        env["QT_QPA_PLATFORM"] = "offscreen"
        env["PYTHONPATH"] = str(PROJECT_ROOT)
        process = subprocess.run(
            [sys.executable, str(CASES), case],
            cwd=str(PROJECT_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        self.assertEqual(
            process.returncode,
            0,
            msg=(
                f"子进程失败（{case}）\nstdout:\n{process.stdout}"
                f"\nstderr:\n{process.stderr}"
            ),
        )
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"子进程没有输出 JSON：{case}")
        payload = json.loads(lines[-1])
        self.assertTrue(payload.get("ok"), payload)
        for marker in FATAL_MARKERS:
            self.assertNotIn(marker, process.stderr, f"{case} 出现致命标记：{marker}")
        return payload, process.stderr

    # a) 布局比例 / 最小宽度 / 按钮完整
    def test_a_layout_ratio_and_min_size(self) -> None:
        payload, _stderr = self._run("layout")
        self.assertGreaterEqual(payload["left_ratio"], 0.25)
        self.assertLessEqual(payload["left_ratio"], 0.33)
        self.assertGreaterEqual(payload["right_ratio"], 0.67)
        self.assertLessEqual(payload["right_ratio"], 0.75)
        self.assertGreaterEqual(
            payload["min_left_width"], config.WORKBENCH_LEFT_MIN_WIDTH
        )
        self.assertGreaterEqual(
            payload["min_right_width"], config.WORKBENCH_RIGHT_MIN_WIDTH
        )
        self.assertTrue(payload["word_wrap"])
        self.assertTrue(payload["footer_separate"])
        self.assertEqual(len(payload["button_widths"]), 4)
        for width in payload["button_widths"]:
            self.assertGreaterEqual(width, payload["button_min"])

    # b) 左栏纯文本、三行文案、无 icon/pixmap
    def test_b_text_only_left_list(self) -> None:
        payload, _stderr = self._run("list_text")
        self.assertEqual(payload["item_count"], 3)
        self.assertEqual(payload["first_line"], "第 1/3 组 ⏳待处理")
        self.assertIn("b.png", payload["third_line"])
        self.assertTrue(payload["third_line"].endswith("等 3 张"))
        self.assertTrue(payload["icon_null"])
        self.assertEqual(payload["left_ratio_pixmaps"], 0)

    # c) 选中样式 / 已处理变灰与 ✅
    def test_c_selected_and_processed_styles(self) -> None:
        payload, _stderr = self._run("styles")
        self.assertEqual(payload["selected_bg"], "#E8F0FE")
        self.assertEqual(payload["hover_bg"], "#F1F6FE")
        self.assertNotEqual(payload["selected_bg"], payload["hover_bg"])
        self.assertEqual(payload["selected_width"], 0)
        self.assertEqual(payload["bar_color"], "#3d6df2")
        self.assertEqual(payload["bar_width"], 2)
        self.assertGreater(payload["radius"], 0)
        self.assertEqual(payload["processed_icon"], "✅")
        self.assertEqual(payload["current_row"], 1)

    # d) 保留/跳过后自动推进与空状态
    def test_d_auto_advance_and_complete_state(self) -> None:
        payload, _stderr = self._run("advance")
        self.assertTrue(payload["complete"])
        self.assertIn("确认 1 组", payload["summary"])
        self.assertIn("移动 1 张", payload["summary"])
        self.assertIn("跳过 2 组", payload["summary"])
        self.assertTrue(payload["stack_is_empty"])

    # e) 点击任意项切换右侧
    def test_e_click_any_group_switches_cards(self) -> None:
        payload, _stderr = self._run("switch")
        self.assertEqual(payload["cards_after_row2"], 2)
        self.assertEqual(payload["cards_after_row1"], 3)
        self.assertEqual(payload["current_index"], 1)

    # f) 内存约束：切组后旧 pixmap 全部释放
    def test_f_pixmap_release_on_group_switch(self) -> None:
        payload, _stderr = self._run("memory")
        self.assertLessEqual(payload["alive_for_current"], payload["current_count"])
        self.assertTrue(payload["old_refs_dead"])

    # g) 决策映射 / X 关闭挂起 / 零移动 / 幂等
    def test_g_decisions_and_suspend_on_close(self) -> None:
        payload, _stderr = self._run("decisions")
        self.assertTrue(payload["confirm_moved"])
        self.assertTrue(payload["skip_no_move"])
        self.assertTrue(payload["suspend_no_move"])
        self.assertTrue(payload["duplicate_no_move"])
        self.assertEqual(
            payload["calls"],
            [
                ["confirm", 0],
                ["skip", 1],
                ["suspend", 2],
                ["suspend", 3],
            ],
        )

    # h) 服务层批处理：一次交批、汇总、LIFO、取消
    def test_h_service_batch_mode(self) -> None:
        payload, _stderr = self._run("service_batch")
        self.assertEqual(payload["groups_once"], 4)
        summary = payload["summary"]
        self.assertEqual(summary["confirmed"], 2)
        self.assertEqual(summary["moved"], 2)
        self.assertEqual(summary["skipped"], 1)
        self.assertEqual(summary["suspended"], 1)
        self.assertEqual(summary["skipped_images"], 2)
        self.assertTrue(payload["duplicate_protected"])
        self.assertTrue(payload["lifo_ok"])
        self.assertTrue(payload["cancel_zero_move"])
        self.assertEqual(payload["cancel_suspended"], 2)

    # i) 空状态汇总、只读查看、关闭清理
    def test_i_empty_state_and_cleanup(self) -> None:
        payload, _stderr = self._run("empty_cleanup")
        self.assertEqual(payload["title"], "全部处理完毕")
        self.assertIn("确认 0 组", payload["summary"])
        self.assertIn("移动 0 张", payload["summary"])
        self.assertIn("跳过 2 组", payload["summary"])
        self.assertTrue(payload["readonly_disabled"])
        self.assertTrue(payload["cleanup"])

    # j) 卡片/按钮样式回归
    def test_j_card_and_button_style_regression(self) -> None:
        payload, _stderr = self._run("regression")
        self.assertEqual(payload["card_radius"], 8)
        self.assertEqual(payload["selected_border"], 2)
        self.assertTrue(payload["shadow_ok"])
        self.assertTrue(payload["primary_tristate"])
        self.assertTrue(payload["secondary_border_1px"])

    # k) UI 美化：分界线 / 面板留白 / 选中态 / 项间距 / 卡片居中 / 底部按钮
    def test_k_ui_polish(self) -> None:
        payload, _stderr = self._run("ui_polish")
        self.assertEqual(payload["handle_width"], 1)
        self.assertEqual(payload["handle_color"], "#E0E0E0")
        self.assertEqual(payload["separators"], 0)
        self.assertGreaterEqual(payload["panel_pad"], 20)
        self.assertEqual(payload["selected_bg"], "#E8F0FE")
        self.assertEqual(payload["hover_bg"], "#F1F6FE")
        self.assertEqual(payload["selected_width"], 0)
        self.assertEqual(payload["bar_width"], 2)
        self.assertGreater(payload["radius"], 0)
        self.assertGreaterEqual(payload["item_gap"], 4)
        self.assertLessEqual(
            abs(payload["card_top_gap"] - payload["card_bottom_gap"]), 4
        )
        self.assertGreaterEqual(payload["button_bottom_gap"], 16)
        self.assertGreaterEqual(payload["button_gap"], 8)
        self.assertLessEqual(payload["button_gap"], 12)
        self.assertTrue(payload["primary_tristate"])
        self.assertTrue(payload["secondary_border_1px"])
        self.assertTrue(payload["complete"])
        self.assertTrue(payload["readonly_disabled"])
        self.assertTrue(payload["skipped_no_move"])

    # r) 整组丢弃：确认文案含真实张数、取消零移动、确认后 discard_all
    def test_r_discard_all_ui(self) -> None:
        payload, _stderr = self._run("discard_ui")
        self.assertIn("3张", payload["confirm_text"])
        self.assertIn("重复文件", payload["confirm_text"])
        self.assertEqual(payload["calls"], [["discard_all", None]])
        self.assertEqual(payload["discarded"], 1)
        self.assertEqual(payload["moved"], 3)
        self.assertEqual(payload["advanced_row"], 1)
        self.assertIn("已处理（全部丢弃）", payload["status_text"])

    # s) 撤销本组：保留移动后恢复、可再次决策与再次撤销
    def test_s_undo_group_after_confirm(self) -> None:
        payload, _stderr = self._run("undo_group_confirm")
        self.assertEqual(payload["status_after_undo"], "pending")
        self.assertTrue(payload["restored"])
        self.assertTrue(payload["redecision_moved"])
        self.assertEqual(payload["undo_stack_after_reundo"], 0)
        # 撤销后按钮恢复可决策，撤销按钮回灰。
        self.assertEqual(payload["button_states"], [True, False])

    # t) 撤销本组：整组丢弃后全部还原
    def test_t_undo_group_after_discard_all(self) -> None:
        payload, _stderr = self._run("undo_group_discard")
        self.assertEqual(payload["status_after_undo"], "pending")
        self.assertTrue(payload["all_restored"])
        self.assertTrue(payload["trash_removed"])
        self.assertEqual(payload["summary"]["discarded"], 0)
        self.assertEqual(payload["summary"]["moved"], 0)

    # u) 最后一组整组丢弃 → 空态撤销按钮点亮 → 点击回到对比界面
    def test_u_undo_last_group_from_complete(self) -> None:
        payload, _stderr = self._run("undo_last_group")
        self.assertTrue(payload["button_before"], "空态下撤销按钮仍应为 Enabled")
        self.assertEqual(payload["status_after"], "pending")
        self.assertTrue(payload["right_is_scroll"], "右侧应回到该组对比界面")
        self.assertEqual(payload["list_row"], 0)
        self.assertFalse(payload["complete_after"])
        self.assertFalse(payload["undo_after"], "还原后按钮应置灰")
        self.assertEqual(payload["summary"]["discarded"], 0)
        self.assertEqual(payload["summary"]["moved"], 0)

    # v) 底部提示独占一行 + 超长文本压力，不与按钮重叠
    def test_v_footer_layout_stress(self) -> None:
        payload, _stderr = self._run("footer_stress")
        self.assertTrue(payload["separate_layouts"])
        self.assertTrue(payload["word_wrap"])
        self.assertTrue(payload["no_overlap"], "提示文字与按钮行发生重叠")
        self.assertGreater(
            payload["hint_height"],
            payload["line_height"],
            "超长提示没有换行增高",
        )
        self.assertEqual(len(payload["button_widths"]), 4)
        for width in payload["button_widths"]:
            self.assertGreaterEqual(width, payload["button_min"])
        row_width = sum(payload["button_widths"]) + 3 * payload["button_gap"]
        self.assertGreaterEqual(row_width, payload["row_min"])

    # l) 卡片控件树：无单选、无「保留这一张」，仅缩略图/文件名/大小
    def test_l_card_control_tree(self) -> None:
        payload, _stderr = self._run("card_tree")
        self.assertEqual(payload["cards"], 3)
        self.assertEqual(payload["children_per_card"], 3)
        self.assertEqual(payload["radio_count"], 0)
        self.assertEqual(len(payload["sizes"]), 3)
        for text in payload["sizes"].values():
            self.assertRegex(text, r"^\d+(\.\d+)? [KMGT]?B$")

    # m) 单击卡片切换选中；旧卡恢复；confirm 使用选中 keeper
    def test_m_card_click_selection_and_keeper(self) -> None:
        payload, _stderr = self._run("card_selection")
        self.assertNotEqual(payload["default"], payload["selected"])
        self.assertTrue(payload["old_restored"])
        self.assertEqual(payload["confirm_keeper"], payload["selected"])

    # n) 按压动画：绘制缩 ≥1px；rect delta 0；LayoutRequest 0；连击不残留
    def test_n_card_press_animation_keeps_layout(self) -> None:
        payload, _stderr = self._run("card_press_anim")
        self.assertGreaterEqual(payload["shrunk_w"], 1)
        self.assertGreaterEqual(payload["shrunk_h"], 1)
        self.assertEqual(payload["rect_delta"], 0)
        self.assertEqual(payload["layout_requests"], 0)
        self.assertGreaterEqual(payload["clicks"], 20)
        self.assertEqual(payload["anim_baseline"], 1)

    # o) 单击=选中、双击=开大图，不互吞事件
    def test_o_card_single_and_double_click_coexist(self) -> None:
        payload, _stderr = self._run("card_double_click")
        self.assertTrue(payload["single_selected"])
        self.assertTrue(payload["single_no_viewer"])
        self.assertEqual(payload["double_viewers"], 1)
        self.assertEqual(payload["pixmap"], [64, 64])

    # p) 键盘可达：卡片可聚焦，Tab 链可达，空格/回车选中
    def test_p_card_keyboard_selection(self) -> None:
        payload, _stderr = self._run("card_keyboard")
        self.assertEqual(payload["focus_policy"], int(Qt.FocusPolicy.StrongFocus))
        self.assertTrue(payload["tab_reachable"])
        self.assertNotEqual(payload["space_selected"], payload["enter_selected"])
        self.assertEqual(payload["focus_border"], config.ACCENT)

    # q) 紧凑卡片：长名省略、大小完整、内边距取 config
    def test_q_card_compact_long_name(self) -> None:
        payload, _stderr = self._run("card_long_name")
        self.assertEqual(payload["pad"], config.WORKBENCH_CARD_PAD)
        self.assertEqual(
            payload["available"], config.REVIEW_CARD_WIDTH - 2 * config.WORKBENCH_CARD_PAD
        )
        self.assertTrue(payload["name_diverges"])
        self.assertEqual(payload["name_tail"], ".png")
        self.assertRegex(payload["size"], r"^\d+(\.\d+)? [KMGT]?B$")


if __name__ == "__main__":
    unittest.main()
