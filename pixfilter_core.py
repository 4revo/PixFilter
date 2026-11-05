"""PixFilter 核心逻辑。

只依赖 Pillow 与 imagehash，不依赖任何 GUI，便于单独测试。

安全约定：
- 本模块只移动文件（shutil.move），绝不调用删除 API。
- 移动失败时保留原文件，并把错误交给调用方记录。
- “重复文件”目录内的文件永远不会被当作扫描候选。
- scan_folder 只负责检测，是否移动由用户逐组确认后调用 apply_review_decision。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

import imagehash
from PIL import Image

# 本地可信目录工具：全景照片、扫描件等超大图很常见。
# Pillow 默认约 8900 万像素的上限会把 1.78 亿像素的正常照片判为
# DecompressionBombError，导致整轮扫描中断。
# 取舍：关闭默认像素上限，改为在打开/解码处逐张捕获异常并跳过。
# 风险：失去解压炸弹的像素上限保护，恶意构造的超大图可能耗尽内存，
# 因此本工具只应处理用户自己信任的本地目录。
Image.MAX_IMAGE_PIXELS = None

SUPPORTED_SUFFIXES = {".jpg", ".jpeg", ".png"}
TRASH_DIR_NAME = "重复文件"

DEFAULT_HASH_SIZE = 8

# 超过该像素数的 JPEG 在算 pHash 前先用 draft() 降低解码分辨率，
# 避免把上亿像素完整读入内存。
DRAFT_THRESHOLD_PIXELS = 32_000_000

# 单张图片读取/解码可能出现的异常集合。
# DecompressionBombWarning 只有 warnings 被设为 error 时才抛出，一并兜住；
# Exception 放在最后，保证任何单张失败都不会中断整轮扫描。
IMAGE_READ_ERRORS = (
    Image.DecompressionBombError,
    Image.DecompressionBombWarning,
    MemoryError,
    OSError,
    Exception,
)
# 64 位 pHash 的汉明距离阈值。8 比较稳妥：能覆盖缩放、重新压缩、
# 轻微调色；继续调高会开始误合并“构图相近但内容不同”的照片。
DEFAULT_DISTANCE = 8
MAX_DISTANCE = DEFAULT_HASH_SIZE * DEFAULT_HASH_SIZE

MD5_CHUNK_SIZE = 1024 * 1024
MISSING_FILE_ERROR = "文件不存在"
MISSING_KEEPER_ERROR = "保留项文件不存在"

LogFn = Callable[[str, str], None]
StopFn = Callable[[], bool]


@dataclass
class ImageInfo:
    path: Path
    rel_path: Path
    file_size: int
    md5: str = ""
    phash: Optional[imagehash.ImageHash] = None
    phash_int: Optional[int] = None
    width: int = 0
    height: int = 0

    @property
    def pixels(self) -> int:
        return self.width * self.height


@dataclass
class DuplicateGroup:
    kind: str  # "exact" 或 "similar"
    keeper: ImageInfo
    duplicates: list[ImageInfo]
    max_distance: int = 0

    @property
    def total(self) -> int:
        return 1 + len(self.duplicates)


@dataclass
class SuspendedGroup:
    """被用户用 X 挂起、稍后仍可继续处理的一组图片。"""

    group: DuplicateGroup
    index: int
    total: int
    root: Path
    uid: int = 0


@dataclass
class MoveResult:
    source: Path
    destination: Optional[Path]
    ok: bool
    error: str = ""


@dataclass
class ScanReport:
    root: Path
    scanned: int = 0
    exact_groups: list[DuplicateGroup] = field(default_factory=list)
    similar_groups: list[DuplicateGroup] = field(default_factory=list)
    moves: list[MoveResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False
    skipped_groups: int = 0
    suspended_count: int = 0
    skipped_images: int = 0

    @property
    def success_moves(self) -> list[MoveResult]:
        return [m for m in self.moves if m.ok]

    @property
    def failed_moves(self) -> list[MoveResult]:
        return [m for m in self.moves if not m.ok]

    @property
    def moved_count(self) -> int:
        return len(self.success_moves)

    def summary_text(self) -> str:
        text = (
            f"共扫描 {self.scanned} 张图片；"
            f"精确重复 {len(self.exact_groups)} 组；"
            f"相似 {len(self.similar_groups)} 组；"
            f"已移入“{TRASH_DIR_NAME}” {self.moved_count} 个"
        )
        if self.skipped_groups:
            text += f"；跳过 {self.skipped_groups} 组"
        if self.skipped_images:
            text += f"；跳过 {self.skipped_images} 张无法读取"
        if self.suspended_count:
            text += f"；挂起 {self.suspended_count} 组"
        if self.failed_moves:
            text += f"；移动失败 {len(self.failed_moves)} 个（原文件已保留）"
        if self.warnings:
            text += f"；告警 {len(self.warnings)} 条"
        if self.cancelled:
            text += "；扫描已取消"
        return text + "。"


def _emit(log: Optional[LogFn], message: str, kind: str = "info") -> None:
    if log is not None:
        log(message, kind)


def _skip_unreadable(
    report: ScanReport,
    log: Optional[LogFn],
    rel_path: Path,
    exc: BaseException,
    *,
    reason: str = "",
) -> None:
    """单张图片读取失败时的统一处理：计数、告警、警告级日志，扫描继续。"""
    detail = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
    suffix = f"，{reason}" if reason else ""
    message = f"跳过无法读取的图片：{rel_path}（{detail}{suffix}）"
    report.skipped_images += 1
    report.warnings.append(message)
    _emit(log, f"⚠️ {message}", "skip")


def iter_image_files(root: Path | str) -> list[Path]:
    """递归收集图片；跳过任意层级的“重复文件”目录。"""
    root_path = Path(root)
    files: list[Path] = []

    for dirpath, dirnames, filenames in os.walk(root_path):
        dirnames[:] = sorted(
            [d for d in dirnames if d.casefold() != TRASH_DIR_NAME.casefold()],
            key=str.casefold,
        )
        for name in sorted(filenames, key=str.casefold):
            if Path(name).suffix.casefold() in SUPPORTED_SUFFIXES:
                path = Path(dirpath) / name
                if path.is_file():
                    files.append(path)

    return files


def file_md5(path: Path | str) -> str:
    digest = hashlib.md5()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(MD5_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_image_size(path: Path | str) -> tuple[int, int]:
    """只读取图片头获取宽高，避免为确认弹窗重复解码整图。"""
    with Image.open(path) as image:
        return image.size


def compute_phash(path: Path | str, hash_size: int = DEFAULT_HASH_SIZE) -> tuple[imagehash.ImageHash, int, int, int]:
    """返回 (pHash 对象, 整数形式, 宽, 高)。无法解码时抛出异常。

    超大 JPEG 会先用 draft() 以低分辨率解码，避免把上亿像素完整读入内存；
    draft() 对 PNG 等格式无效，这类图片仍可能占用较多内存，
    由调用方捕获 MemoryError 后跳过。
    """
    with Image.open(path) as image:
        width, height = image.size
        if image.format == "JPEG" and width * height > DRAFT_THRESHOLD_PIXELS:
            try:
                image.draft("L", (max(32, hash_size * 8), max(32, hash_size * 8)))
            except (AttributeError, ValueError, OSError):
                pass
        phash = imagehash.phash(image, hash_size=hash_size)
    # 十六进制字符串与布局无关，整数异或即可得到汉明距离。
    return phash, int(str(phash), 16), width, height


def pick_exact_keeper(items: Iterable[ImageInfo]) -> ImageInfo:
    """完全重复组：路径排序取第一张（大小写不敏感且稳定）。"""
    return sorted(items, key=lambda info: str(info.path).casefold())[0]


def pick_similar_keeper(items: Iterable[ImageInfo]) -> ImageInfo:
    """相似组：分辨率最高优先，其次文件体积最大，最后路径排序第一。"""
    return sorted(
        items,
        key=lambda info: (-info.pixels, -info.file_size, str(info.path).casefold()),
    )[0]


def find_exact_groups(infos: Iterable[ImageInfo]) -> list[DuplicateGroup]:
    by_md5: dict[str, list[ImageInfo]] = defaultdict(list)
    for info in infos:
        if info.md5:
            by_md5[info.md5].append(info)

    groups: list[DuplicateGroup] = []
    for items in by_md5.values():
        if len(items) > 1:
            keeper = pick_exact_keeper(items)
            duplicates = sorted(
                (item for item in items if item.path != keeper.path),
                key=lambda info: str(info.path).casefold(),
            )
            groups.append(DuplicateGroup("exact", keeper, duplicates))

    return sorted(groups, key=lambda group: str(group.keeper.path).casefold())


def _hamming(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _candidate_pairs(values: list[int], threshold: int, bit_length: int) -> set[tuple[int, int]]:
    """用鸽巢分桶得到“可能相似”的索引对。

    把 64 位哈希切成 threshold + 1 段：距离 <= threshold 的两个哈希，
    必然至少有一段完全相同，因此只需在同一段桶内做精确比较。
    这样既能保证不漏，又避免全量 O(n²)。
    """
    pairs: set[tuple[int, int]] = set()
    block_count = threshold + 1

    for block in range(block_count):
        low = block * bit_length // block_count
        high = (block + 1) * bit_length // block_count
        width = high - low
        if width <= 0:
            continue
        mask = (1 << width) - 1
        buckets: dict[int, list[int]] = defaultdict(list)
        for index, value in enumerate(values):
            buckets[(value >> low) & mask].append(index)
        for bucket in buckets.values():
            if len(bucket) < 2:
                continue
            for left_pos in range(len(bucket)):
                for right_pos in range(left_pos + 1, len(bucket)):
                    pairs.add((bucket[left_pos], bucket[right_pos]))

    return pairs


def find_similar_groups(
    infos: Iterable[ImageInfo],
    threshold: int = DEFAULT_DISTANCE,
    hash_size: int = DEFAULT_HASH_SIZE,
) -> list[DuplicateGroup]:
    """按 pHash 汉明距离聚类（并查集，传递闭包），返回含多余项的分组。"""
    bit_length = hash_size * hash_size
    if threshold < 0:
        raise ValueError("相似度阈值不能为负数")
    if threshold > bit_length:
        raise ValueError(f"相似度阈值不能超过 {bit_length}")

    candidates = [info for info in infos if info.phash_int is not None]
    if len(candidates) < 2:
        return []

    values = [int(info.phash_int) for info in candidates]

    if threshold >= bit_length:
        # 任意两张的差异都不会超过阈值，全部归为一组。
        components = [list(range(len(candidates)))]
    else:
        parent = list(range(len(candidates)))

        def find(index: int) -> int:
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left: int, right: int) -> None:
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for left, right in _candidate_pairs(values, threshold, bit_length):
            if _hamming(values[left], values[right]) <= threshold:
                union(left, right)

        by_root: dict[int, list[int]] = defaultdict(list)
        for index in range(len(candidates)):
            by_root[find(index)].append(index)
        components = [items for items in by_root.values() if len(items) > 1]

    groups: list[DuplicateGroup] = []
    for component in components:
        items = [candidates[index] for index in component]
        keeper = pick_similar_keeper(items)
        max_distance = max(_hamming(int(keeper.phash_int), int(item.phash_int)) for item in items)
        duplicates = sorted(
            (item for item in items if item.path != keeper.path),
            key=lambda info: (-info.pixels, -info.file_size, str(info.path).casefold()),
        )
        groups.append(DuplicateGroup("similar", keeper, duplicates, max_distance))

    return sorted(groups, key=lambda group: str(group.keeper.path).casefold())


def group_members(group: DuplicateGroup) -> list[ImageInfo]:
    """组内全部图片，按路径稳定排序，供并排对比展示。"""
    return sorted(
        [group.keeper, *group.duplicates],
        key=lambda info: str(info.path).casefold(),
    )


def pick_default_keeper(group: DuplicateGroup) -> ImageInfo:
    """复用既有的保留规则，作为确认弹窗的默认选项。

    完全重复组用路径排序第一；相似组用“分辨率最高 → 体积最大 → 路径”。
    """
    members = group_members(group)
    if group.kind == "exact":
        return pick_exact_keeper(members)
    return pick_similar_keeper(members)


def pending_groups(report: ScanReport) -> list[DuplicateGroup]:
    """按扫描顺序列出所有需要人工确认的组。"""
    return [*report.exact_groups, *report.similar_groups]


def unique_destination(destination: Path) -> Path:
    """目标已存在时添加 _1、_2 … 后缀，绝不覆盖。"""
    if not destination.exists():
        return destination
    for index in range(1, 10_000):
        candidate = destination.with_name(f"{destination.stem}_{index}{destination.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"无法为 {destination} 找到不冲突的文件名")


def move_to_trash(root: Path | str, info: ImageInfo) -> Path:
    """把一张图片移动到 <root>/重复文件，保留相对目录结构。"""
    root_path = Path(root)
    destination = root_path / TRASH_DIR_NAME / info.rel_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = unique_destination(destination)

    if not info.path.is_file():
        raise FileNotFoundError(f"源文件不存在：{info.path}")

    shutil.move(str(info.path), str(destination))
    return destination


def apply_review_decision(
    root: Path | str,
    group: DuplicateGroup,
    keeper: ImageInfo,
    execute: bool,
    log: Optional[LogFn] = None,
) -> list[MoveResult]:
    """按人工确认结果执行：execute=False 时一张都不移动。"""
    root_path = Path(root).expanduser().resolve()
    members = group_members(group)
    if all(member.path != keeper.path for member in members):
        raise ValueError(f"保留项不在该组内：{keeper.path}")

    if not execute:
        _emit(
            log,
            f"[跳过] 未移动任何文件："
            f"{'精确重复' if group.kind == 'exact' else '视觉相似'}组，"
            + "、".join(str(item.rel_path) for item in members),
            "warn",
        )
        return []

    if not keeper.path.is_file():
        _emit(
            log,
            f"[缺失] {MISSING_KEEPER_ERROR}，本组未移动任何文件：{keeper.rel_path}",
            "error",
        )
        return [MoveResult(keeper.path, None, False, MISSING_KEEPER_ERROR)]

    _emit(
        log,
        f"[确认保留] {keeper.rel_path}（{_format_info(keeper)}）",
        "keep",
    )
    results: list[MoveResult] = []
    for member in members:
        if member.path == keeper.path:
            continue
        if not member.path.is_file():
            _emit(
                log,
                f"[缺失] 已跳过不存在的文件：{member.rel_path}",
                "warn",
            )
            results.append(MoveResult(member.path, None, False, MISSING_FILE_ERROR))
            continue
        try:
            destination = move_to_trash(root_path, member)
        except Exception as exc:
            message = f"移动失败，原文件已保留：{member.rel_path}（{exc}）"
            results.append(MoveResult(member.path, None, False, str(exc)))
            _emit(log, message, "error")
        else:
            results.append(MoveResult(member.path, destination, True))
            _emit(
                log,
                f"移入“{TRASH_DIR_NAME}”：{member.rel_path} -> "
                f"{destination.relative_to(root_path)}",
                "move",
            )
    return results


def format_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _format_info(info: ImageInfo) -> str:
    if info.width and info.height:
        return f"{info.width}×{info.height}，{format_size(info.file_size)}"
    return format_size(info.file_size)


def _report_progress(log: Optional[LogFn], stage: str, done: int, total: int) -> None:
    if total <= 0:
        return
    step = max(1, total // 10)
    if done == total or done == 1 or done % step == 0:
        _emit(log, f"  {stage} 进度 {done}/{total}", "info")


def scan_folder(
    folder: Path | str,
    *,
    distance: int = DEFAULT_DISTANCE,
    hash_size: int = DEFAULT_HASH_SIZE,
    log: Optional[LogFn] = None,
    should_stop: Optional[StopFn] = None,
) -> ScanReport:
    """扫描并检测重复/相似组，不自动移动任何文件。

    log(message, kind) 会收到所有用户可见的中文进度；
    should_stop() 返回 True 时尽快停止检测并返回已发现的分组。
    """
    root = Path(folder).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"目录不存在或不可访问：{root}")
    if root.name.casefold() == TRASH_DIR_NAME.casefold():
        raise ValueError(f"不能把“{TRASH_DIR_NAME}”目录本身作为扫描目录")
    if not 0 <= distance <= hash_size * hash_size:
        raise ValueError(f"相似度阈值应在 0 到 {hash_size * hash_size} 之间")

    report = ScanReport(root=root)
    stop = should_stop or (lambda: False)

    files = iter_image_files(root)
    report.scanned = len(files)
    _emit(log, f"开始扫描：{root}", "info")
    _emit(log, f"发现 {len(files)} 张候选图片（.jpg/.jpeg/.png，已跳过“{TRASH_DIR_NAME}”）", "info")

    # 第一轮：MD5
    _emit(log, "第一阶段：计算 MD5，查找完全重复…", "info")
    infos: list[ImageInfo] = []
    for index, path in enumerate(files, 1):
        if stop():
            report.cancelled = True
            _emit(log, "扫描已取消，未移动任何文件。", "warn")
            return report

        rel_path = path.relative_to(root)
        try:
            size = path.stat().st_size
        except IMAGE_READ_ERRORS as exc:
            _skip_unreadable(report, log, rel_path, exc, reason="读取文件属性失败")
            _report_progress(log, "MD5", index, len(files))
            continue
        try:
            # 先读文件头拿宽高：超大图/损坏图能在算 MD5 之前就被跳过。
            width, height = read_image_size(path)
        except IMAGE_READ_ERRORS as exc:
            _skip_unreadable(report, log, rel_path, exc, reason="读取图片尺寸失败")
            _report_progress(log, "MD5", index, len(files))
            continue
        try:
            md5 = file_md5(path)
        except IMAGE_READ_ERRORS as exc:
            _skip_unreadable(report, log, rel_path, exc, reason="计算 MD5 失败")
            _report_progress(log, "MD5", index, len(files))
            continue

        infos.append(
            ImageInfo(
                path=path,
                rel_path=rel_path,
                file_size=size,
                md5=md5,
                width=width,
                height=height,
            )
        )
        _report_progress(log, "MD5", index, len(files))

    report.exact_groups = find_exact_groups(infos)
    if report.exact_groups:
        _emit(log, f"精确重复：{len(report.exact_groups)} 组", "group")
    else:
        _emit(log, "精确重复：未发现", "info")

    if stop():
        report.cancelled = True
        _emit(log, "扫描已取消，未移动任何文件。", "warn")
        return report

    # 第二轮：只对“每个内容保留一张”的代表图算 pHash，
    # 已判定的完全重复副本无需重复解码。
    duplicate_paths = {item.path for group in report.exact_groups for item in group.duplicates}
    representatives = [info for info in infos if info.path not in duplicate_paths]

    _emit(log, f"第二阶段：计算 pHash（{hash_size}×{hash_size}），阈值 {distance}…", "info")
    for index, info in enumerate(representatives, 1):
        if stop():
            report.cancelled = True
            _emit(log, "扫描已取消，未移动任何文件。", "warn")
            return report
        try:
            phash, phash_int, width, height = compute_phash(info.path, hash_size=hash_size)
        except IMAGE_READ_ERRORS as exc:
            # 单张 pHash 失败不影响其它图片，也不影响已完成的分组。
            _skip_unreadable(report, log, info.rel_path, exc, reason="计算 pHash 失败")
        else:
            info.phash = phash
            info.phash_int = phash_int
            info.width = width
            info.height = height
        _report_progress(log, "pHash", index, len(representatives))

    report.similar_groups = find_similar_groups(
        representatives, threshold=distance, hash_size=hash_size
    )
    if report.similar_groups:
        _emit(log, f"视觉相似：{len(report.similar_groups)} 组", "group")
    else:
        _emit(log, "视觉相似：未发现", "info")

    if stop():
        report.cancelled = True
        _emit(log, "扫描已取消，未移动任何文件。", "warn")
        return report

    pending_tasks = len(report.exact_groups) + len(report.similar_groups)
    _emit(
        log,
        f"检测完成：精确重复 {len(report.exact_groups)} 组、相似 {len(report.similar_groups)} 组，"
        f"共 {pending_tasks} 组待人工确认。",
        "summary",
    )
    return report
