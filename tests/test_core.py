"""PixFilter 核心逻辑自测（不依赖 GUI）。

运行：python tests/test_core.py -v
所有测试产物都放在临时目录，测试结束由 TemporaryDirectory 自动清理。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pixfilter_core  # noqa: E402
from pixfilter_core import (  # noqa: E402
    MISSING_FILE_ERROR,
    MISSING_KEEPER_ERROR,
    TRASH_DIR_NAME,
    ImageInfo,
    SuspendedGroup,
    apply_review_decision,
    compute_phash,
    group_members,
    move_to_trash,
    pending_groups,
    pick_default_keeper,
    scan_folder,
)


def draw_pattern(path: Path, size: tuple[int, int], style: str, quality: int | None = None) -> None:
    width, height = size
    if style == "a":
        image = Image.new("RGB", size, "#f6f0e2")
        draw = ImageDraw.Draw(image)
        draw.ellipse((width * 0.08, height * 0.12, width * 0.52, height * 0.88), fill="#2b2b2b")
        draw.rectangle((width * 0.62, height * 0.18, width * 0.94, height * 0.72), fill="#2f6dbd")
        draw.line((0, height * 0.92, width, height * 0.92), fill="#b03a2e", width=6)
    elif style == "b":
        image = Image.new("RGB", size, "#18202b")
        draw = ImageDraw.Draw(image)
        for offset in range(-height, width * 2, max(20, width // 18)):
            draw.line((offset, 0, offset + width, height), fill="#e8e3d3", width=max(4, width // 60))
        draw.ellipse((width * 0.1, height * 0.1, width * 0.45, height * 0.55), fill="#c0392b")
        draw.rectangle((width * 0.56, height * 0.55, width * 0.9, height * 0.88), fill="#d7b74a")
    elif style == "c":
        image = Image.new("RGB", size, "#f7f7f7")
        draw = ImageDraw.Draw(image)
        for x in range(0, width, max(6, width // 40)):
            draw.line((x, 0, x, height), fill="#2f3b4a", width=2)
        draw.arc(
            (width * 0.25, height * 0.25, width * 0.75, height * 0.75),
            start=210,
            end=330,
            fill="#e67e22",
            width=max(4, width // 50),
        )
    else:
        raise ValueError(style)

    path.parent.mkdir(parents=True, exist_ok=True)
    if quality is None:
        image.save(path)
    else:
        image.save(path, quality=quality)


class PixFilterCoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="pixfilter_test_")
        self.root = Path(self._temp_dir.name)

        # 完全重复：字节完全一致
        draw_pattern(self.root / "exact" / "base.png", (360, 270), "a")
        shutil.copy2(self.root / "exact" / "base.png", self.root / "exact" / "copy.png")

        # 视觉相似：高分辨率原图缩小 + JPEG 重压缩
        original = self.root / "similar" / "original.png"
        draw_pattern(original, (960, 720), "b")
        with Image.open(original) as image:
            small = image.resize((480, 360), Image.Resampling.LANCZOS)
        small.save(self.root / "similar" / "recompressed.jpg", "JPEG", quality=40)

        # 独立图片
        draw_pattern(self.root / "other" / "independent.png", (400, 400), "c")

        # 损坏图片：应告警并跳过
        (self.root / "other" / "broken.jpg").write_bytes(b"\xff\xd8\xff\xe0broken-data")

    def tearDown(self) -> None:
        self._temp_dir.cleanup()

    def _detect(self, **kwargs):
        logs: list[tuple[str, str]] = []
        report = scan_folder(
            self.root,
            distance=kwargs.get("distance", 8),
            log=lambda message, kind: logs.append((kind, message)),
            should_stop=kwargs.get("should_stop"),
        )
        return report, logs

    def test_detection_does_not_move_and_defaults_follow_existing_rules(self) -> None:
        report, _ = self._detect()

        self.assertEqual(report.scanned, 6)
        self.assertEqual(len(report.exact_groups), 1)
        self.assertEqual(len(report.similar_groups), 1)
        self.assertEqual(report.moved_count, 0)
        self.assertFalse((self.root / TRASH_DIR_NAME).exists())
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("broken.jpg", report.warnings[0])

        exact = report.exact_groups[0]
        similar = report.similar_groups[0]
        self.assertEqual(pick_default_keeper(exact).rel_path, Path("exact/base.png"))
        self.assertEqual(pick_default_keeper(similar).rel_path, Path("similar/original.png"))
        self.assertEqual([item.rel_path for item in group_members(exact)], [
            Path("exact/base.png"),
            Path("exact/copy.png"),
        ])
        # 完全重复副本也有尺寸信息，方便确认弹窗展示。
        self.assertTrue(all(item.width > 0 and item.height > 0 for item in group_members(exact)))

    def test_skip_decision_moves_nothing(self) -> None:
        report, logs = self._detect()
        group = report.exact_groups[0]
        results = apply_review_decision(
            self.root,
            group,
            pick_default_keeper(group),
            execute=False,
            log=lambda message, kind: logs.append((kind, message)),
        )

        self.assertEqual(results, [])
        self.assertFalse((self.root / TRASH_DIR_NAME).exists())
        for relative in (
            "exact/base.png",
            "exact/copy.png",
            "similar/original.png",
            "similar/recompressed.jpg",
            "other/independent.png",
            "other/broken.jpg",
        ):
            self.assertTrue((self.root / relative).is_file(), relative)
        self.assertTrue(any("跳过" in message for _, message in logs))

    def test_confirm_moves_exactly_the_other_members(self) -> None:
        report, logs = self._detect()
        exact = report.exact_groups[0]
        similar = report.similar_groups[0]

        exact_results = apply_review_decision(
            self.root, exact, pick_default_keeper(exact), execute=True,
            log=lambda message, kind: logs.append((kind, message)),
        )
        self.assertEqual(len(exact_results), 1)
        self.assertTrue(all(result.ok for result in exact_results))
        report.moves.extend(exact_results)
        self.assertTrue((self.root / "exact" / "base.png").is_file())
        self.assertTrue((self.root / TRASH_DIR_NAME / "exact" / "copy.png").is_file())
        self.assertFalse((self.root / "exact" / "copy.png").exists())

        similar_results = apply_review_decision(
            self.root, similar, pick_default_keeper(similar), execute=True,
            log=lambda message, kind: logs.append((kind, message)),
        )
        self.assertEqual(len(similar_results), 1)
        report.moves.extend(similar_results)
        self.assertTrue((self.root / "similar" / "original.png").is_file())
        self.assertTrue(
            (self.root / TRASH_DIR_NAME / "similar" / "recompressed.jpg").is_file()
        )
        self.assertFalse((self.root / "similar" / "recompressed.jpg").exists())

        # 独立图片与损坏图片不受影响
        self.assertTrue((self.root / "other" / "independent.png").is_file())
        self.assertTrue((self.root / "other" / "broken.jpg").is_file())
        self.assertFalse((self.root / TRASH_DIR_NAME / "other" / "independent.png").exists())

        text = "\n".join(message for _, message in logs)
        self.assertIn(f"移入“{TRASH_DIR_NAME}”：{Path('exact/copy.png')}", text)
        self.assertIn(f"移入“{TRASH_DIR_NAME}”：{Path('similar/recompressed.jpg')}", text)
        self.assertEqual(report.moved_count, 2)
        self.assertIn(f"已移入“{TRASH_DIR_NAME}” 2 个", report.summary_text())

    def test_confirm_a_non_default_keeper_moves_only_the_other(self) -> None:
        report, _ = self._detect()
        group = report.exact_groups[0]
        chosen = next(item for item in group_members(group) if item.path.name == "copy.png")

        results = apply_review_decision(self.root, group, chosen, execute=True)

        self.assertEqual(len(results), 1)
        self.assertTrue((self.root / "exact" / "copy.png").is_file())
        self.assertFalse((self.root / "exact" / "base.png").exists())
        self.assertTrue((self.root / TRASH_DIR_NAME / "exact" / "base.png").is_file())

    def test_rescan_never_moves_files_from_duplicate_folder_again(self) -> None:
        report, _ = self._detect()
        for group in (*report.exact_groups, *report.similar_groups):
            apply_review_decision(self.root, group, pick_default_keeper(group), execute=True)

        rescan, _ = self._detect()
        self.assertEqual(rescan.scanned, 4)  # “重复文件”内 2 张被排除
        self.assertEqual(rescan.moved_count, 0)
        self.assertEqual(len(rescan.exact_groups), 0)
        self.assertEqual(len(rescan.similar_groups), 0)
        self.assertTrue(
            (self.root / TRASH_DIR_NAME / "exact" / "copy.png").is_file()
        )
        self.assertTrue(
            (self.root / TRASH_DIR_NAME / "similar" / "recompressed.jpg").is_file()
        )

    def test_name_conflict_gets_suffix_without_overwrite(self) -> None:
        source = self.root / "a" / "x.png"
        draw_pattern(source, (100, 100), "c")
        trashed = self.root / TRASH_DIR_NAME / "a" / "x.png"
        trashed.parent.mkdir(parents=True)
        trashed.write_bytes(b"old-content")

        info = ImageInfo(path=source, rel_path=Path("a/x.png"), file_size=source.stat().st_size)
        destination = move_to_trash(self.root, info)

        self.assertEqual(destination.name, "x_1.png")
        self.assertTrue(destination.is_file())
        self.assertFalse(source.exists())
        self.assertEqual(trashed.read_bytes(), b"old-content")

    def test_stop_before_detection_moves_nothing(self) -> None:
        report, _ = self._detect(should_stop=lambda: True)

        self.assertTrue(report.cancelled)
        self.assertEqual(report.moved_count, 0)
        self.assertFalse((self.root / TRASH_DIR_NAME).exists())
        for relative in (
            "exact/base.png",
            "exact/copy.png",
            "similar/original.png",
            "similar/recompressed.jpg",
            "other/independent.png",
            "other/broken.jpg",
        ):
            self.assertTrue((self.root / relative).is_file(), relative)

    def test_single_image_produces_no_pending_group(self) -> None:
        solo = self.root / "solo"
        draw_pattern(solo / "only.png", (240, 180), "a")
        report = scan_folder(solo, log=lambda message, kind: None)

        self.assertEqual(report.scanned, 1)
        self.assertEqual(report.exact_groups, [])
        self.assertEqual(report.similar_groups, [])
        # 单张候选没有可确认的组，界面因此不会弹窗。
        self.assertEqual(pending_groups(report), [])

    def test_suspended_group_keeps_data_for_later_processing(self) -> None:
        report, _ = self._detect()
        group = report.exact_groups[0]
        suspended = SuspendedGroup(group=group, index=1, total=1, root=self.root)

        # 挂起只保存数据，不动文件。
        self.assertEqual(
            [item.rel_path for item in group_members(suspended.group)],
            [Path("exact/base.png"), Path("exact/copy.png")],
        )
        self.assertTrue(
            all(item.width > 0 and item.height > 0 and item.file_size > 0
                for item in group_members(suspended.group))
        )
        keeper = pick_default_keeper(suspended.group)
        self.assertEqual(keeper.rel_path, Path("exact/base.png"))
        self.assertTrue(keeper.path.is_file())
        self.assertTrue((self.root / "exact" / "copy.png").is_file())
        self.assertFalse((self.root / TRASH_DIR_NAME).exists())

        # 稍后继续处理时，仍按同一规则算出保留项与落选项。
        results = apply_review_decision(
            suspended.root, suspended.group, keeper, execute=True
        )
        self.assertEqual([result.source.name for result in results], ["copy.png"])
        self.assertTrue((self.root / "exact" / "base.png").is_file())
        self.assertFalse((self.root / "exact" / "copy.png").exists())
        self.assertTrue(
            (self.root / TRASH_DIR_NAME / "exact" / "copy.png").is_file()
        )

    def test_abandon_suspended_group_moves_nothing(self) -> None:
        report, logs = self._detect()
        suspended = SuspendedGroup(
            group=report.exact_groups[0], index=1, total=1, root=self.root
        )

        results = apply_review_decision(
            suspended.root,
            suspended.group,
            pick_default_keeper(suspended.group),
            execute=False,
            log=lambda message, kind: logs.append((kind, message)),
        )

        self.assertEqual(results, [])
        self.assertFalse((self.root / TRASH_DIR_NAME).exists())
        for relative in ("exact/base.png", "exact/copy.png", "similar/original.png"):
            self.assertTrue((self.root / relative).is_file(), relative)
        self.assertTrue(any("跳过" in message for _, message in logs))

    def test_missing_group_member_is_skipped_with_explicit_result(self) -> None:
        report, _ = self._detect()
        group = report.exact_groups[0]
        keeper = pick_default_keeper(group)
        missing = next(item for item in group.duplicates if item.path.name == "copy.png")

        # 模拟该文件已被其他组操作移动走。
        move_to_trash(self.root, missing)
        self.assertFalse(missing.path.exists())
        self.assertTrue(
            (self.root / TRASH_DIR_NAME / missing.rel_path).is_file()
        )

        results = apply_review_decision(self.root, group, keeper, execute=True)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].error, MISSING_FILE_ERROR)
        self.assertTrue(keeper.path.is_file())
        self.assertFalse(
            (self.root / TRASH_DIR_NAME / "exact" / "base.png").exists()
        )

    def test_missing_keeper_aborts_group_without_moving_others(self) -> None:
        report, _ = self._detect()
        group = report.exact_groups[0]
        keeper = pick_default_keeper(group)

        # 模拟保留项已被其他组操作移动走。
        move_to_trash(self.root, keeper)
        self.assertFalse(keeper.path.is_file())

        results = apply_review_decision(self.root, group, keeper, execute=True)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertEqual(results[0].error, MISSING_KEEPER_ERROR)
        self.assertTrue((self.root / "exact" / "copy.png").is_file())
        self.assertFalse(
            (self.root / TRASH_DIR_NAME / "exact" / "copy.png").exists()
        )

    def test_phash_distances_are_separated(self) -> None:
        original_hash = compute_phash(self.root / "similar" / "original.png")
        recompressed_hash = compute_phash(self.root / "similar" / "recompressed.jpg")
        independent_hash = compute_phash(self.root / "other" / "independent.png")

        def distance(left, right) -> int:
            return (left[1] ^ right[1]).bit_count()

        similar_distance = distance(original_hash, recompressed_hash)
        independent_distance = distance(original_hash, independent_hash)
        print(
            f"\npHash 距离：original↔recompressed={similar_distance}，"
            f"original↔independent={independent_distance}"
        )
        self.assertLessEqual(similar_distance, 8)
        self.assertGreater(independent_distance, 8)

    def test_decompression_bomb_is_skipped_and_others_still_grouped(self) -> None:
        """复现超大图像素上限报错：只跳过该图，其余图片照常去重。"""
        bomb = self.root / "huge" / "panorama.png"
        draw_pattern(bomb, (2000, 2000), "c")  # 400 万像素

        # Pillow 在 open() 时读取模块级 MAX_IMAGE_PIXELS；临时调小即可复现。
        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 1_000_000):
            report, logs = self._detect()

        self.assertEqual(report.scanned, 7)
        self.assertEqual(len(report.exact_groups), 1)
        self.assertEqual(len(report.similar_groups), 1)
        self.assertGreaterEqual(report.skipped_images, 1)
        self.assertIn("张无法读取", report.summary_text())

        grouped_paths = [
            item.path
            for group in (*report.exact_groups, *report.similar_groups)
            for item in group_members(group)
        ]
        self.assertNotIn(bomb, grouped_paths)

        skip_logs = [message for kind, message in logs if kind == "skip"]
        self.assertTrue(any("panorama.png" in message for message in skip_logs))
        self.assertTrue(all(message.startswith("⚠️") for message in skip_logs))

    def test_corrupt_file_is_skipped_without_aborting(self) -> None:
        corrupt_dir = self.root / "corrupt"
        draw_pattern(corrupt_dir / "good.png", (300, 200), "a")
        (corrupt_dir / "bad.png").write_bytes(b"not-an-image")

        logs: list[tuple[str, str]] = []
        report = scan_folder(
            corrupt_dir, log=lambda message, kind: logs.append((kind, message))
        )

        self.assertEqual(report.scanned, 2)
        self.assertEqual(report.skipped_images, 1)
        self.assertEqual(report.exact_groups, [])
        self.assertEqual(report.similar_groups, [])
        self.assertTrue(
            any(kind == "skip" and "bad.png" in message for kind, message in logs)
        )

    def test_memory_error_on_single_image_is_caught(self) -> None:
        real_reader = pixfilter_core.read_image_size

        def fake_reader(path):
            if Path(path).name == "independent.png":
                raise MemoryError("simulated memory pressure")
            return real_reader(path)

        with mock.patch.object(pixfilter_core, "read_image_size", side_effect=fake_reader):
            report, logs = self._detect()

        self.assertEqual(report.scanned, 6)
        self.assertEqual(len(report.exact_groups), 1)
        self.assertEqual(len(report.similar_groups), 1)
        self.assertEqual(report.skipped_images, 2)  # broken.jpg + independent.png
        self.assertTrue(any("independent.png" in message for message in report.warnings))

    def test_normal_dedupe_regression_keeps_keeper_rules_and_structure(self) -> None:
        report, logs = self._detect()

        self.assertEqual(report.moved_count, 0)
        self.assertEqual(len(report.exact_groups), 1)
        self.assertEqual(len(report.similar_groups), 1)

        exact = report.exact_groups[0]
        similar = report.similar_groups[0]
        self.assertEqual(pick_default_keeper(exact).rel_path, Path("exact/base.png"))
        self.assertEqual(pick_default_keeper(similar).rel_path, Path("similar/original.png"))

        for group in (exact, similar):
            results = apply_review_decision(
                self.root,
                group,
                pick_default_keeper(group),
                True,
                log=lambda message, kind: logs.append((kind, message)),
            )
            report.moves.extend(results)

        self.assertEqual(report.moved_count, 2)
        self.assertTrue((self.root / "exact" / "base.png").is_file())
        self.assertTrue((self.root / "similar" / "original.png").is_file())
        self.assertTrue((self.root / TRASH_DIR_NAME / "exact" / "copy.png").is_file())
        self.assertTrue(
            (self.root / TRASH_DIR_NAME / "similar" / "recompressed.jpg").is_file()
        )
        self.assertFalse((self.root / "exact" / "copy.png").exists())
        self.assertFalse((self.root / "similar" / "recompressed.jpg").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
