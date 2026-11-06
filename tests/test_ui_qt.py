"""Qt 主界面测试：全部在独立子进程 + offscreen 平台运行。

隔离原因：Qt 需要独立的 QApplication 与平台插件；本模块自身不导入
PySide6，避免主 unittest 进程被 Qt 事件循环污染。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CASE_RUNNER = PROJECT_ROOT / "tests" / "qt_main_cases.py"


def run_case(case: str, extra_env: dict[str, str] | None = None, timeout: float = 15.0):
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    # 平台在子进程环境里强制 offscreen，且放在 extra_env 之后，
    # 任何调用方都无法把测试拉回真实桌面平台。
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    env["PYTHONUNBUFFERED"] = "1"
    result = subprocess.run(
        [sys.executable, str(CASE_RUNNER), case],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result


class QtMainUITest(unittest.TestCase):
    def assert_case_ok(self, case: str, extra_env: dict[str, str] | None = None, timeout=15.0):
        result = run_case(case, extra_env, timeout)
        self.assertEqual(
            result.returncode,
            0,
            f"case {case} 失败\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        self.assertIn(f"OK {case}", result.stdout)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_a_widgets_copy_and_initial_state(self) -> None:
        self.assert_case_ok("a")

    def test_b_threshold_linkage_and_int_value(self) -> None:
        self.assert_case_ok("b")

    def test_c_log_path_click_copies_and_skip_red(self) -> None:
        self.assert_case_ok("c")

    def test_d_pending_panel_paths_and_file_destinations(self) -> None:
        self.assert_case_ok("d", timeout=20.0)

    def test_e_review_cards_default_placeholder_viewer_and_decisions(self) -> None:
        self.assert_case_ok("e")

    def test_f_toast_geometry_fade_and_non_blocking(self) -> None:
        self.assert_case_ok("f", timeout=20.0)

    def test_g_github_url_and_errors(self) -> None:
        self.assert_case_ok("g")

    def test_h_min_window_geometry_no_overlap(self) -> None:
        self.assert_case_ok("h")

    def test_i_dpi_100_125_200(self) -> None:
        for scale in ("1", "1.25", "2"):
            with self.subTest(scale=scale):
                result = self.assert_case_ok(
                    "i",
                    {
                        "QT_SCALE_FACTOR": scale,
                        "PIXFILTER_TEST_SCALE": scale,
                    },
                )
                self.assertIn(f"scale={float(scale):g}", result.stdout)

    def test_j_close_cleanup(self) -> None:
        self.assert_case_ok("j", timeout=20.0)

    def test_k_scrollbar_and_folder_lock(self) -> None:
        self.assert_case_ok("scroll_folder", timeout=30.0)

    def test_l_scrollbars_hidden_when_not_needed(self) -> None:
        self.assert_case_ok("scroll_empty", timeout=40.0)

    def test_m_pending_table_select_double_click_and_single_workbench(self) -> None:
        self.assert_case_ok("pending_workbench", timeout=45.0)

    def test_n_doctor_offscreen_pending_line_and_progress(self) -> None:
        result = self.assert_case_ok("doctor_offscreen", timeout=30.0)
        self.assertIn("progress distance=", result.stdout)

    def test_p_toast_round_offscreen(self) -> None:
        for scale in ("1", "1.25", "2"):
            with self.subTest(scale=scale):
                result = self.assert_case_ok(
                    "toast_round",
                    {"QT_SCALE_FACTOR": scale},
                    timeout=40.0,
                )
                self.assertIn("dominant=(76, 175, 80)", result.stdout)
                self.assertIn("corners=[(0, 0, 0, 0), (0, 0, 0, 0)", result.stdout)

    def test_q_toast_round_pixels_offscreen(self) -> None:
        for scale in ("1", "1.25", "2"):
            with self.subTest(scale=scale):
                result = self.assert_case_ok(
                    "toast_round",
                    {"QT_SCALE_FACTOR": scale},
                    timeout=45.0,
                )
                self.assertIn("platform='offscreen'", result.stdout)
                self.assertIn("corners=[(0, 0, 0, 0), (0, 0, 0, 0)", result.stdout)
                self.assertIn("dominant=(76, 175, 80)", result.stdout)

    def test_o_doctor_pixels_pending_line_and_progress(self) -> None:
        result = self.assert_case_ok("doctor_pixels", timeout=45.0)
        self.assertIn("platform='offscreen'", result.stdout)
        self.assertIn("max_run=1", result.stdout)

    def test_r_button_press_bounce_animation(self) -> None:
        result = self.assert_case_ok("press_bounce", timeout=40.0)
        self.assertIn("platform='offscreen'", result.stdout)
        self.assertIn("rect_delta=0", result.stdout)
        self.assertIn("layout_requests=0", result.stdout)
        self.assertIn("clicks=32", result.stdout)
        match = re.search(r"shrink_w=(\d+) shrink_h=(\d+)", result.stdout)
        self.assertIsNotNone(match, result.stdout)
        self.assertGreaterEqual(int(match.group(1)), 1)
        self.assertGreaterEqual(int(match.group(2)), 1)

    def test_s_pending_table_row_hover(self) -> None:
        result = self.assert_case_ok("table_hover", timeout=40.0)
        self.assertIn("platform='offscreen'", result.stdout)
        self.assertIn("hover=(242, 246, 253)", result.stdout)
        self.assertIn("selected=(232, 240, 254)", result.stdout)
        self.assertIn("max_delta=0", result.stdout)
        self.assertIn("input=['synthetic']", result.stdout)
        match = re.search(r"moved_updates=(\d+)", result.stdout)
        self.assertIsNotNone(match, result.stdout)
        self.assertGreaterEqual(int(match.group(1)), 19)
        self.assertLessEqual(int(match.group(1)), 21)

    def test_t_long_name_elide_keeps_size(self) -> None:
        result = self.assert_case_ok("long_name", timeout=60.0)
        self.assertIn("platform='offscreen'", result.stdout)
        self.assertIn("long_tail='aaa…'", result.stdout)
        row_match = re.search(r"table_row_h=(\d+)", result.stdout)
        self.assertIsNotNone(row_match, result.stdout)
        self.assertGreater(int(row_match.group(1)), 0)
        match = re.search(r"gap=([\d.]+)", result.stdout)
        self.assertIsNotNone(match, result.stdout)
        self.assertGreaterEqual(float(match.group(1)), 8.0)

    def test_u_abandon_selected_only_removes_selected(self) -> None:
        result = self.assert_case_ok("abandon_selected", timeout=40.0)
        self.assertIn(
            "abandon_selected rows=2 files=6 log_growth=1",
            result.stdout,
        )

    def test_v_abandon_all_four_branches(self) -> None:
        result = self.assert_case_ok("abandon_all", timeout=40.0)
        self.assertIn("cancel_rows=5 cancel_log_delta=0", result.stdout)
        self.assertIn("confirm_rows=0", result.stdout)

    def test_w_pending_footer_layout(self) -> None:
        result = self.assert_case_ok("pending_footer_layout", timeout=40.0)
        self.assertIn("footer w=1040", result.stdout)
        self.assertIn("footer w=900", result.stdout)
        self.assertIn("right_margin=18", result.stdout)
        for match in re.finditer(
            r"y_delta=([\d.]+) gaps=\[(\d+), (\d+)\]",
            result.stdout,
        ):
            self.assertLessEqual(float(match.group(1)), 2.0)
            for gap in (int(match.group(2)), int(match.group(3))):
                self.assertGreaterEqual(gap, 8)
                self.assertLessEqual(gap, 12)

    def test_stop_status_and_button_states(self) -> None:
        self.assert_case_ok("stop", timeout=20.0)

    def test_entry_runs_qt_event_loop(self) -> None:
        self.assert_case_ok("entry", timeout=20.0)

    def test_scan_then_undo_end_to_end(self) -> None:
        self.assert_case_ok("scan_e2e", timeout=30.0)

    def test_port_contract_is_implemented_by_view(self) -> None:
        self.assert_case_ok("ports", timeout=20.0)

    def test_controller_wiring_scan_workbench_undo(self) -> None:
        self.assert_case_ok("controller_wiring", timeout=40.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
