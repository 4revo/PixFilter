"""相似聚类的鸽巢分块索引：等价性、传递性、兜底与性能基准。

全部基于合成 pHash 整数，不打开任何真实图片、不创建窗口、不涉及鼠标。
基准测试使用 1100 条哈希（随机噪声 + 完全重复拷贝 + 近似变体），
只衡量比对/聚类阶段；哈希计算阶段不属于本次优化范围。

运行：python tests/test_similar_index.py -v
"""

from __future__ import annotations

import random
import sys
import time
import unittest
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pixfilter_core import (  # noqa: E402
    MAX_INDEX_BLOCKS,
    MIN_INDEX_ITEMS,
    ImageInfo,
    SimilarSearchStats,
    _build_segment_buckets,
    _collect_candidate_pairs,
    _segment_plan,
    find_similar_groups,
    group_members,
)

ALL_ONES = (1 << 64) - 1
# 每 16 位块各置 2 位：总距离 8，且四个 16 位块与全 0 都不同。
SPREAD_EIGHT = 0x0003_0003_0003_0003


def make_infos(values: list[int], prefix: str = "img") -> list[ImageInfo]:
    """把哈希整数包成 ImageInfo；分辨率/体积随索引变化，用于校验 keeper 规则。"""
    infos: list[ImageInfo] = []
    for index, value in enumerate(values):
        side = 64 + (index % 7) * 8
        infos.append(
            ImageInfo(
                path=Path(f"C:/pix/{prefix}_{index:04d}.png"),
                rel_path=Path(f"{prefix}_{index:04d}.png"),
                file_size=1000 + (index % 11) * 37,
                phash_int=value,
                width=side,
                height=side,
            )
        )
    return infos


def group_signature(groups) -> list[tuple]:
    """完整快照：分组顺序、keeper、成员顺序、重复项顺序与最大距离都要一致。"""
    return [
        (
            group.kind,
            str(group.keeper.path),
            tuple(str(info.path) for info in group_members(group)),
            tuple(str(info.path) for info in group.duplicates),
            group.max_distance,
        )
        for group in groups
    ]


def edge_values(count: int = 120, seed: int = 20261106) -> list[int]:
    """边界值 + 随机值 + 重复拷贝 + 单比特/八比特近似变体。"""
    rng = random.Random(seed)
    values: list[int] = [
        0, 0, 0,  # 全 0 完全相同
        ALL_ONES, ALL_ONES,  # 全 1 完全相同
        1, 1 << 63,  # 与 0 仅差 1 位
        SPREAD_EIGHT,  # 与 0 差 8 位，四个 16 位块全不同
    ]
    while len(values) < count:
        base = rng.getrandbits(64)
        values.append(base)
        values.append(base)  # 完全重复拷贝
        if len(values) < count:
            if rng.random() < 0.5:
                values.append(base ^ (1 << rng.randrange(64)))
            else:
                values.append(base ^ SPREAD_EIGHT)
    return values[:count]


def old_four_block_candidates(values: list[int]) -> set[tuple[int, int]]:
    """旧方案（4 个 16 位块）的候选对，用来演示 8 位差异会被漏掉。"""
    buckets: list[defaultdict[int, list[int]]] = [defaultdict(list) for _ in range(4)]
    for index, value in enumerate(values):
        for block in range(4):
            buckets[block][(value >> (16 * block)) & 0xFFFF].append(index)
    pairs: set[tuple[int, int]] = set()
    for bucket_map in buckets:
        for bucket in bucket_map.values():
            for left_pos in range(len(bucket)):
                for right_pos in range(left_pos + 1, len(bucket)):
                    pairs.add((bucket[left_pos], bucket[right_pos]))
    return pairs


def build_benchmark_values(base_count: int = 900, variant_count: int = 100) -> list[int]:
    """随机哈希 + 完全重复拷贝 + 每 16 位块各翻 2 位的近似变体。"""
    rng = random.Random(42)
    base = [rng.getrandbits(64) for _ in range(base_count)]
    values = list(base) + list(base[:variant_count])
    for index in range(variant_count):
        flip = 0
        for block in range(4):
            for position in rng.sample(range(16), 2):
                flip |= 1 << (16 * block + position)
        values.append(base[index] ^ flip)
    return values


class SegmentPlanTest(unittest.TestCase):
    def test_segment_plan_uses_threshold_plus_one_segments(self) -> None:
        widths = [width for _, width in _segment_plan(64, 9)]
        self.assertEqual(widths, [7] * 8 + [8])
        self.assertEqual(sum(widths), 64)

    def test_every_close_pair_shares_some_segment(self) -> None:
        """鸽巢原理：距离 <= 阈值的一对，必然至少有一段完全相同。"""
        plan = _segment_plan(64, 9)
        for left, right in ((0, SPREAD_EIGHT), (0, 1), (ALL_ONES, ALL_ONES ^ 0xFF)):
            shared = any(
                ((left >> low) & ((1 << width) - 1)) == ((right >> low) & ((1 << width) - 1))
                for low, width in plan
            )
            self.assertTrue(shared, (left, right))


class TargetedCounterexampleTest(unittest.TestCase):
    def test_old_four_block_scheme_would_miss_eight_bit_pair(self) -> None:
        """0 与 SPREAD_EIGHT 差 8 位且四块全不同：旧方案不产生候选对。"""
        values = [0, SPREAD_EIGHT]
        self.assertNotIn((0, 1), old_four_block_candidates(values))

        new_pairs = _collect_candidate_pairs(
            _build_segment_buckets(values, _segment_plan(64, 9))
        )
        self.assertIn((0, 1), new_pairs)

    def test_new_scheme_keeps_eight_bit_pair_in_one_group(self) -> None:
        infos = make_infos([0, SPREAD_EIGHT])
        groups = find_similar_groups(infos, threshold=8, strategy="index")
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0].duplicates), 1)

        # 阈值 7 时距离 8 不成立，不能并入同一组。
        groups = find_similar_groups(infos, threshold=7, strategy="index")
        self.assertEqual(groups, [])


class EquivalenceTest(unittest.TestCase):
    def assert_paths_equal(self, values: list[int], threshold: int) -> None:
        infos = make_infos(values)
        brute = find_similar_groups(infos, threshold=threshold, strategy="brute")
        indexed = find_similar_groups(infos, threshold=threshold, strategy="index")
        auto = find_similar_groups(infos, threshold=threshold, strategy="auto")
        self.assertEqual(group_signature(brute), group_signature(indexed))
        self.assertEqual(group_signature(brute), group_signature(auto))

    def test_equivalence_across_thresholds(self) -> None:
        values = edge_values()
        for threshold in (1, 3, 5, 8, 12, 20):
            with self.subTest(threshold=threshold):
                self.assert_paths_equal(values, threshold)

    def test_equivalence_all_zeros_and_all_ones(self) -> None:
        values = [0] * 30 + [ALL_ONES] * 30
        for threshold in (1, 3, 5, 8, 12, 20):
            with self.subTest(threshold=threshold):
                self.assert_paths_equal(values, threshold)

    def test_equivalence_many_identical_hashes(self) -> None:
        values = [0x1234_5678_9ABC_DEF0] * 40
        self.assert_paths_equal(values, 1)
        self.assert_paths_equal(values, 8)

    def test_equivalence_single_bit_variants(self) -> None:
        base = 0xDEAD_BEEF_DEAD_BEEF
        values = [base] + [base ^ (1 << bit) for bit in range(10)]
        for threshold in (1, 3, 8):
            with self.subTest(threshold=threshold):
                self.assert_paths_equal(values, threshold)


class TransitivityTest(unittest.TestCase):
    def test_indirect_neighbors_end_up_in_one_group(self) -> None:
        # A=0；B 置低 8 位（A~B=8）；C 低 5 位 + 8..12 位共 10 位。
        # A~C=10 > 8，B~C=8，因此必须通过 B 传递合并。
        a, b, c = 0, 0xFF, 0x1F | (0x1F << 8)
        infos = make_infos([a, b, c])
        for strategy in ("brute", "index", "auto"):
            with self.subTest(strategy=strategy):
                groups = find_similar_groups(infos, threshold=8, strategy=strategy)
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0].total, 3)

    def test_chain_of_ten_images_stays_one_group(self) -> None:
        values = [0]
        for step in range(1, 10):
            values.append(values[-1] ^ (0xFF << (8 * ((step - 1) % 8))))
        # 相邻两张恰好相差 8 位，首尾相差远超阈值。
        infos = make_infos(values)
        for strategy in ("brute", "index", "auto"):
            with self.subTest(strategy=strategy):
                groups = find_similar_groups(infos, threshold=8, strategy=strategy)
                self.assertEqual(len(groups), 1)
                self.assertEqual(groups[0].total, 10)


class FallbackTest(unittest.TestCase):
    def random_distinct(self, count: int, seed: int = 99) -> list[int]:
        rng = random.Random(seed)
        values: set[int] = set()
        while len(values) < count:
            values.add(rng.getrandbits(64))
        return list(values)

    def test_auto_falls_back_below_fifty_items(self) -> None:
        stats = SimilarSearchStats()
        infos = make_infos(self.random_distinct(MIN_INDEX_ITEMS - 1))
        find_similar_groups(infos, threshold=8, strategy="auto", stats=stats)
        self.assertEqual(stats.mode, "brute")
        self.assertEqual(stats.fallback_reason, "too_few_items")
        self.assertEqual(stats.index_seconds, 0.0)

    def test_auto_uses_index_at_fifty_items(self) -> None:
        stats = SimilarSearchStats()
        infos = make_infos(self.random_distinct(MIN_INDEX_ITEMS))
        find_similar_groups(infos, threshold=8, strategy="auto", stats=stats)
        self.assertEqual(stats.mode, "index")
        self.assertEqual(stats.block_count, 9)
        self.assertGreater(stats.total_seconds, 0.0)

    def test_auto_falls_back_when_more_than_sixteen_segments(self) -> None:
        values = self.random_distinct(60)
        infos = make_infos(values)

        stats = SimilarSearchStats()
        find_similar_groups(infos, threshold=MAX_INDEX_BLOCKS, strategy="auto", stats=stats)
        self.assertEqual(stats.block_count, MAX_INDEX_BLOCKS + 1)

        stats = SimilarSearchStats()
        find_similar_groups(
            infos, threshold=MAX_INDEX_BLOCKS + 1, strategy="auto", stats=stats
        )
        self.assertEqual(stats.block_count, MAX_INDEX_BLOCKS + 2)
        self.assertEqual(stats.mode, "brute")
        self.assertEqual(stats.fallback_reason, "too_many_blocks")
        self.assertEqual(stats.index_seconds, 0.0)

    def test_auto_falls_back_when_candidates_cover_most_pairs(self) -> None:
        values = [0] * 60
        infos = make_infos(values)
        stats = SimilarSearchStats()
        find_similar_groups(infos, threshold=8, strategy="auto", stats=stats)
        self.assertEqual(stats.mode, "brute")
        self.assertEqual(stats.fallback_reason, "low_gain")

        brute = find_similar_groups(infos, threshold=8, strategy="brute")
        indexed = find_similar_groups(infos, threshold=8, strategy="index")
        self.assertEqual(group_signature(brute), group_signature(indexed))

    def test_forced_strategies_override_heuristics(self) -> None:
        infos = make_infos([0, 0, 1])
        stats = SimilarSearchStats()
        find_similar_groups(infos, threshold=8, strategy="index", stats=stats)
        self.assertEqual(stats.mode, "index")
        self.assertEqual(stats.fallback_reason, "")
        self.assertGreater(stats.candidate_count, 0)

        stats = SimilarSearchStats()
        find_similar_groups(infos, threshold=8, strategy="brute", stats=stats)
        self.assertEqual(stats.mode, "brute")
        self.assertEqual(stats.fallback_reason, "forced")

    def test_invalid_strategy_rejected(self) -> None:
        infos = make_infos([0, 1])
        with self.assertRaises(ValueError):
            find_similar_groups(infos, threshold=8, strategy="unknown")


class BenchmarkTest(unittest.TestCase):
    def test_thousand_plus_images_reports_speedup_and_matches(self) -> None:
        values = build_benchmark_values()
        self.assertGreaterEqual(len(values), 1000)
        infos = make_infos(values)

        brute_stats = SimilarSearchStats()
        brute_start = time.perf_counter()
        brute_groups = find_similar_groups(
            infos, threshold=8, strategy="brute", stats=brute_stats
        )
        brute_wall = time.perf_counter() - brute_start

        index_stats = SimilarSearchStats()
        index_start = time.perf_counter()
        index_groups = find_similar_groups(
            infos, threshold=8, strategy="index", stats=index_stats
        )
        index_wall = time.perf_counter() - index_start

        self.assertEqual(group_signature(brute_groups), group_signature(index_groups))

        speedup = brute_wall / index_wall if index_wall else 0.0
        print(
            f"\n[benchmark] images={len(values)} "
            f"brute={brute_wall * 1000:.1f}ms index={index_wall * 1000:.1f}ms "
            f"speedup={speedup:.2f}x groups={len(brute_groups)} "
            f"candidates={index_stats.candidate_count}"
            f" (index-build={index_stats.index_seconds * 1000:.1f}ms, "
            f"collect={index_stats.candidate_seconds * 1000:.1f}ms, "
            f"verify={index_stats.verify_seconds * 1000:.1f}ms)"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
