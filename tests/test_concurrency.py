"""Concurrency, determinism, progress and cancellation tests (no visible windows)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pixfilter_core
from pixfilter_core import (
    _resolve_max_workers,
    group_members,
    pick_default_keeper,
    scan_folder,
)


def draw_pattern(path: Path, size=(180, 140), seed: int = 0, quality: int = 90) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new(
        "RGB", size, ((seed * 37) % 256, (seed * 59 + 40) % 256, (seed * 83 + 90) % 256)
    )
    drawer = ImageDraw.Draw(image)
    for i in range(6):
        x0 = (seed * 17 + i * 23) % max(1, size[0] - 30)
        y0 = (seed * 41 + i * 19) % max(1, size[1] - 30)
        drawer.rectangle(
            [x0, y0, x0 + 24, y0 + 24],
            fill=((seed * 11 + i * 31) % 256, (i * 47) % 256, (seed * 7 + 120) % 256),
        )
    drawer.text((4, 4), f"S{seed}", fill=(255, 255, 255))
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        image.save(path, quality=quality)
    else:
        image.save(path)
    return path


def copy_exact(source: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return target


def signature(report) -> list[tuple]:
    groups = []
    for group in (*report.exact_groups, *report.similar_groups):
        groups.append(
            (
                group.kind,
                str(group.keeper.rel_path),
                str(pick_default_keeper(group).rel_path),
                tuple(str(item.rel_path) for item in group.duplicates),
                group.total,
                [str(item.rel_path) for item in group_members(group)],
            )
        )
    return groups


class ConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="pixfilter_concurrency_")
        self.root = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _make_deterministic_set(self) -> Path:
        folder = self.root / "det"
        base = draw_pattern(folder / "exact" / "a.png", seed=1)
        copy_exact(base, folder / "exact" / "a_copy.png")
        copy_exact(base, folder / "exact" / "a_copy2.png")
        original_path = draw_pattern(folder / "similar" / "original.png", seed=2)
        with Image.open(original_path) as image:
            image.save(folder / "similar" / "recompressed.jpg", quality=55)
        for seed in range(3, 8):
            draw_pattern(folder / "unique" / f"u{seed}.png", seed=seed)
        return folder

    def test_workers_one_and_eight_are_identical(self) -> None:
        folder = self._make_deterministic_set()
        serial = scan_folder(folder, max_workers=1)
        concurrent = scan_folder(folder, max_workers=8)
        concurrent_again = scan_folder(folder, max_workers=8)

        self.assertEqual(signature(serial), signature(concurrent))
        self.assertEqual(signature(concurrent), signature(concurrent_again))
        self.assertEqual(serial.scanned, concurrent.scanned)
        self.assertEqual(serial.skipped_images, concurrent.skipped_images)

    def test_bad_and_oversized_images_are_skipped_in_parallel(self) -> None:
        folder = self.root / "bad"
        for seed in range(1, 7):
            draw_pattern(folder / f"good{seed}.png", seed=seed)
        copy_exact(folder / "good1.png", folder / "good1_copy.png")
        copy_exact(folder / "good2.png", folder / "good2_copy.png")
        (folder / "broken.jpg").write_bytes(b"not-an-image")
        huge = folder / "huge.png"
        Image.new("RGB", (2000, 2000), (12, 34, 56)).save(huge)

        with mock.patch.object(Image, "MAX_IMAGE_PIXELS", 1_000_000):
            report = scan_folder(folder, max_workers=8)

        self.assertEqual(report.scanned, 10)
        self.assertGreaterEqual(report.skipped_images, 2)
        self.assertEqual(len(report.exact_groups), 2)
        self.assertTrue(any("huge.png" in item for item in report.warnings))
        self.assertTrue(any("broken.jpg" in item for item in report.warnings))

    def test_progress_is_monotonic_per_stage(self) -> None:
        folder = self.root / "progress"
        base = draw_pattern(folder / "a.png", seed=10)
        copy_exact(base, folder / "a_copy.png")
        for seed in range(11, 21):
            draw_pattern(folder / f"u{seed}.png", seed=seed)

        events: list[tuple[str, int, int]] = []
        report = scan_folder(folder, max_workers=4, progress_cb=lambda *args: events.append(args))

        self.assertEqual(report.scanned, 12)
        for stage in ("md5", "phash"):
            values = [(done, total) for name, done, total in events if name == stage]
            self.assertTrue(values, f"{stage} 没有进度回调")
            self.assertEqual(values[0][0], 1)
            self.assertEqual(values[-1][0], values[-1][1])
            for previous, current in zip(values, values[1:]):
                self.assertGreater(current[0], previous[0])
            self.assertEqual({total for _, total in values}, {values[-1][1]})

    def test_cancel_returns_fast_without_moving_files(self) -> None:
        folder = self.root / "cancel"
        for seed in range(40):
            draw_pattern(folder / f"p{seed}.png", seed=seed, size=(320, 240))

        event = threading.Event()

        def progress(stage: str, done: int, total: int) -> None:
            if stage == "md5" and done >= 20:
                event.set()

        started = time.perf_counter()
        report = scan_folder(
            folder,
            max_workers=8,
            cancel_event=event,
            progress_cb=progress,
        )
        elapsed = time.perf_counter() - started

        self.assertTrue(report.cancelled)
        self.assertLess(elapsed, 2.0)
        self.assertFalse((folder / pixfilter_core.TRASH_DIR_NAME).exists())
        self.assertEqual(len(list(folder.iterdir())), 40)
        self.assertEqual(report.moved_count, 0)

    def test_500_files_no_loss(self) -> None:
        folder = self.root / "stress"
        for index in range(250):
            base = folder / f"b{index:03d}.png"
            base.parent.mkdir(parents=True, exist_ok=True)
            image = Image.new("RGB", (48, 48), (index % 256, (index * 3) % 256, (index * 7) % 256))
            drawer = ImageDraw.Draw(image)
            drawer.text((4, 4), str(index), fill=(255, 255, 255))
            image.save(base)
            copy_exact(base, folder / f"b{index:03d}_copy.png")

        report = scan_folder(folder, max_workers=8)
        self.assertEqual(report.scanned, 500)
        self.assertEqual(report.skipped_images, 0)
        self.assertEqual(len(report.exact_groups), 250)
        self.assertEqual(sum(group.total for group in report.exact_groups), 500)

    def test_worker_count_formula_and_override(self) -> None:
        self.assertEqual(
            _resolve_max_workers(None), min(16, max(4, os.cpu_count() or 4))
        )
        self.assertEqual(_resolve_max_workers(8), 8)
        self.assertEqual(_resolve_max_workers(1000), 64)
        self.assertEqual(_resolve_max_workers(0), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
