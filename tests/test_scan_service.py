"""ScanService 纯逻辑测试：不创建任何 GUI，不依赖显示器。"""

from __future__ import annotations

import hashlib
import inspect
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import scan_service
from pixfilter_core import (
    DuplicateGroup,
    ImageInfo,
    ScanReport,
    TRASH_DIR_NAME,
)


def _info(root: Path, name: str) -> ImageInfo:
    path = root / name
    return ImageInfo(
        path=path,
        rel_path=Path(name),
        file_size=path.stat().st_size if path.is_file() else 1,
    )


def _group(root: Path, first: str, second: str) -> DuplicateGroup:
    keeper = _info(root, first)
    duplicate = _info(root, second)
    return DuplicateGroup(kind="exact", keeper=keeper, duplicates=[duplicate])


class ScanServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory(prefix="pixfilter_service_")
        self.folder = Path(self._temp.name)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _wait(self, event: threading.Event, timeout: float = 5.0) -> None:
        self.assertTrue(event.wait(timeout), "服务回调未在超时内触发")

    # a) 无 GUI 环境导入即可用
    def test_a_imports_and_idles_without_gui(self) -> None:
        service = scan_service.ScanService()
        self.assertEqual(service.state, "idle")
        self.assertFalse(service.has_undo)
        self.assertEqual(service.pending_groups, ())
        self.assertIsNone(service.folder)

    # b) 进度单调 + finished 只触发一次
    def test_b_progress_monotonic_and_finished_once(self) -> None:
        progress: list[tuple[str, int, int]] = []
        finished: list[ScanReport] = []
        finished_event = threading.Event()

        def fake_scan(
            root, *, distance, log=None, should_stop=None, cancel_event=None,
            progress_cb=None, max_workers=None, **kwargs,
        ):
            for value in (1, 2, 2, 1, 3):
                if progress_cb:
                    progress_cb("md5", value, 10)
            for value in (1, 0, 2):
                if progress_cb:
                    progress_cb("phash", value, 5)
            return ScanReport(root=Path(root), scanned=10)

        service = scan_service.ScanService(
            progress_cb=lambda stage, done, total: progress.append(
                (stage, done, total)
            ),
            finished_cb=lambda report: (finished.append(report), finished_event.set()),
        )
        with mock.patch.object(scan_service, "scan_folder", fake_scan):
            self.assertTrue(service.start_scan(self.folder, 8))
            self._wait(finished_event)
            deadline = time.perf_counter() + 1.0
            while service.state != "idle" and time.perf_counter() < deadline:
                time.sleep(0.01)

        self.assertEqual(len(finished), 1)
        self.assertEqual(service.state, "idle")
        self.assertEqual(
            progress,
            [("md5", 1, 10), ("md5", 2, 10), ("md5", 3, 10), ("phash", 1, 5), ("phash", 2, 5)],
        )

    # c) 取消：快速返回、无移动、状态回 idle
    def test_c_cancel_is_fast_and_moves_nothing(self) -> None:
        started = threading.Event()
        finished: list[ScanReport] = []
        finished_event = threading.Event()

        def fake_scan(
            root, *, distance, log=None, should_stop=None, cancel_event=None,
            progress_cb=None, max_workers=None, **kwargs,
        ):
            total = 200
            for index in range(1, total + 1):
                started.set()
                if (cancel_event is not None and cancel_event.is_set()) or (
                    should_stop is not None and should_stop()
                ):
                    return ScanReport(root=Path(root), scanned=index, cancelled=True)
                if progress_cb:
                    progress_cb("md5", index, total)
                time.sleep(0.005)
            return ScanReport(root=Path(root), scanned=total)

        service = scan_service.ScanService(
            finished_cb=lambda report: (finished.append(report), finished_event.set())
        )
        with mock.patch.object(scan_service, "scan_folder", fake_scan):
            self.assertTrue(service.start_scan(self.folder, 8))
            self._wait(started)
            begin = time.perf_counter()
            service.request_cancel()
            self._wait(finished_event)
            elapsed = time.perf_counter() - begin

        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(finished), 1)
        self.assertTrue(finished[0].cancelled)
        self.assertEqual(finished[0].moves, [])
        self.assertEqual(service.state, "idle")

    # d) 撤销栈：两批 LIFO、撤销后栈空
    def test_d_undo_stack_is_lifo(self) -> None:
        trash = self.folder / TRASH_DIR_NAME
        trash.mkdir()
        first_source = self.folder / "first.txt"
        second_source = self.folder / "second.txt"
        first_source.write_text("first", encoding="utf-8")
        second_source.write_text("second", encoding="utf-8")
        first_dest = trash / "first.txt"
        second_dest = trash / "second.txt"
        first_source.rename(first_dest)
        second_source.rename(second_dest)

        service = scan_service.ScanService()
        service.record_batch([(first_source, first_dest)])
        service.record_batch([(second_source, second_dest)])
        self.assertEqual(len(service.undo_stack), 2)
        self.assertTrue(service.has_undo)

        events: list[tuple] = []
        done = threading.Event()

        def on_undo(batch, restored, skipped, cleanup):
            events.append((len(batch), restored, skipped, cleanup))
            done.set()

        service._undo_finished_cb = on_undo

        self.assertTrue(service.undo_last_batch())
        self._wait(done)
        # LIFO：先恢复第二批
        self.assertTrue(second_source.exists())
        self.assertFalse(first_source.exists())
        self.assertEqual(len(service.undo_stack), 1)
        self.assertEqual(service.state, "idle")

        done.clear()
        self.assertTrue(service.undo_last_batch())
        self._wait(done)
        self.assertTrue(first_source.exists())
        self.assertEqual(service.undo_stack, [])
        self.assertFalse(service.has_undo)

    # e) 空回收站清理：仅根目录 rmdir、非空保留、异常轻提示
    def test_e_cleanup_only_root_and_handles_errors(self) -> None:
        trash = self.folder / TRASH_DIR_NAME
        trash.mkdir()
        logs: list[tuple[str, str]] = []
        service = scan_service.ScanService(
            log_cb=lambda message, kind: logs.append((message, kind))
        )
        self.assertEqual(
            service.cleanup_empty_trash(self.folder, trash, lambda m, k: logs.append((m, k))),
            "removed",
        )
        self.assertFalse(trash.exists())

        trash.mkdir()
        (trash / "keep.txt").write_text("x", encoding="utf-8")
        self.assertEqual(
            service.cleanup_empty_trash(self.folder, trash, lambda m, k: logs.append((m, k))),
            "kept",
        )
        self.assertTrue(trash.exists())

        (trash / "keep.txt").unlink()
        with mock.patch.object(
            scan_service.os, "rmdir", side_effect=OSError("in use")
        ):
            status = service.cleanup_empty_trash(
                self.folder, trash, lambda m, k: logs.append((m, k))
            )
        self.assertEqual(status, "error")
        self.assertTrue(any(kind == "warn" for _message, kind in logs))

        self.assertEqual(
            service.cleanup_empty_trash(self.folder, self.folder / "other", lambda *_a: None),
            "invalid",
        )
        self.assertEqual(service.cleanup_empty_trash(None, None, lambda *_a: None), "missing")

    # f) suspend / confirm / 放弃的决策流
    def test_f_suspend_confirm_and_abandon_flow(self) -> None:
        first_a = self.folder / "a1.bin"
        first_b = self.folder / "a2.bin"
        second_a = self.folder / "b1.bin"
        second_b = self.folder / "b2.bin"
        first_a.write_bytes(b"same-a")
        first_b.write_bytes(b"same-a")
        second_a.write_bytes(b"same-b")
        second_b.write_bytes(b"same-b")

        groups = [
            _group(self.folder, "a1.bin", "a2.bin"),
            _group(self.folder, "b1.bin", "b2.bin"),
        ]
        report = ScanReport(root=self.folder, scanned=4, exact_groups=groups)
        suspended: list = []
        finished: list[ScanReport] = []
        finished_event = threading.Event()
        service = None

        def on_group(group, index, total, root):
            self.assertEqual(service.state, "reviewing")
            # 第一组挂起，第二组也挂起后由测试分别 confirm/放弃。
            service.submit_decision("suspend", None)

        fake_calls = {"n": 0}

        def fake_scan(
            root, *, distance, log=None, should_stop=None, cancel_event=None,
            progress_cb=None, max_workers=None, **kwargs,
        ):
            fake_calls["n"] += 1
            return report

        service = scan_service.ScanService(
            group_ready_cb=on_group,
            suspended_cb=lambda item: suspended.append(item),
            finished_cb=lambda rep: (finished.append(rep), finished_event.set()),
        )
        with mock.patch.object(scan_service, "scan_folder", fake_scan):
            self.assertTrue(service.start_scan(self.folder, 8))
            self._wait(finished_event)

        self.assertEqual(fake_calls["n"], 1)
        self.assertEqual(len(finished), 1)
        self.assertEqual(finished[0].suspended_count, 2)
        self.assertEqual(len(service.pending_groups), 2)
        self.assertEqual(len(suspended), 2)

        # confirm 第一组：dup 移入“重复文件”，keeper 保留；pending 减少。
        first = service.pending_groups[0]
        outcome = service.apply_decision(
            first.group, "confirm", first.group.keeper, root=first.root
        )
        self.assertTrue(outcome.moves)
        self.assertTrue(first.group.keeper.path.exists())
        self.assertTrue((self.folder / TRASH_DIR_NAME).is_dir())
        service.remove_suspended(first)
        self.assertEqual(len(service.pending_groups), 1)
        self.assertEqual(len(service.undo_stack), 1)

        # 放弃第二组：零移动、仅移除待处理。
        second = service.pending_groups[0]
        outcome = service.apply_decision(
            second.group, "skip", second.group.keeper, root=second.root
        )
        self.assertEqual(outcome.moves, [])
        service.remove_suspended(second)
        self.assertEqual(service.pending_groups, ())
        self.assertTrue(second_a.exists() and second_b.exists())
        self.assertEqual(service.state, "idle")

    # ---------- 撤销与清理的服务层等价断言（原 Tk 用例迁移） ----------
    def _make_trash_batch(self, names: list[str]) -> list[tuple[Path, Path]]:
        trash = self.folder / TRASH_DIR_NAME
        trash.mkdir()
        batch = []
        for name in names:
            source = self.folder / name
            source.write_bytes(f"content-{name}".encode("utf-8"))
            destination = trash / name
            source.rename(destination)
            batch.append((source, destination))
        return batch

    def _undo_and_wait(self, service: scan_service.ScanService) -> dict:
        service._folder = self.folder  # 真实运行时由扫描/选择目录设置。
        result: dict = {}
        done = threading.Event()

        def on_undo(batch, restored, skipped, cleanup):
            result.update(
                batch=len(batch), restored=restored, skipped=skipped, cleanup=cleanup
            )
            done.set()

        service._undo_finished_cb = on_undo
        self.assertTrue(service.undo_last_batch())
        self._wait(done)
        return result

    # g) 撤销恢复文件内容与原哈希，并在清空后删除回收站目录
    def test_g_undo_restores_content_and_removes_trash(self) -> None:
        batch = self._make_trash_batch(["keep.png", "dup.png"])
        hashes = {
            name: hashlib.sha256((self.folder / TRASH_DIR_NAME / name).read_bytes()).hexdigest()
            for name in ("keep.png", "dup.png")
        }
        logs: list[tuple[str, str]] = []
        service = scan_service.ScanService(
            log_cb=lambda message, kind: logs.append((message, kind))
        )
        service.record_batch(batch)
        outcome = self._undo_and_wait(service)

        self.assertEqual(outcome["restored"], 2)
        self.assertEqual(outcome["skipped"], 0)
        self.assertEqual(outcome["cleanup"], "removed")
        for name, digest in hashes.items():
            path = self.folder / name
            self.assertTrue(path.is_file())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        self.assertFalse((self.folder / TRASH_DIR_NAME).exists())
        self.assertEqual(service.state, "idle")
        self.assertFalse(service.has_undo)
        self.assertTrue(
            sum(1 for message, _ in logs if message.startswith("已恢复")) == 2,
            f"恢复日志数量不符：{logs}",
        )

    # h) 撤销冲突：不覆盖用户新文件，冲突项留在回收站
    def test_h_undo_conflict_never_overwrites(self) -> None:
        batch = self._make_trash_batch(["dup.png"])
        source, destination = batch[0]
        source.write_bytes(b"user-created-new-file")
        user_hash = hashlib.sha256(source.read_bytes()).hexdigest()

        logs: list[tuple[str, str]] = []
        service = scan_service.ScanService(
            log_cb=lambda message, kind: logs.append((message, kind))
        )
        service.record_batch(batch)
        outcome = self._undo_and_wait(service)

        self.assertEqual(outcome["restored"], 0)
        self.assertEqual(outcome["skipped"], 1)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), user_hash)
        self.assertTrue(destination.exists(), "冲突项不应从回收站消失")
        joined = "\n".join(message for message, _ in logs)
        self.assertIn("未覆盖", joined)
        self.assertIn("原位置已存在同名文件", joined)
        self.assertNotIn("无法清理空的重复文件夹", joined)

    # i) 回收站文件已不存在：跳过且不阻塞
    def test_i_undo_missing_trash_file_is_skipped(self) -> None:
        batch = self._make_trash_batch(["dup.png"])
        source, destination = batch[0]
        destination.unlink()

        logs: list[tuple[str, str]] = []
        service = scan_service.ScanService(
            log_cb=lambda message, kind: logs.append((message, kind))
        )
        service.record_batch(batch)
        outcome = self._undo_and_wait(service)

        self.assertEqual(outcome["skipped"], 1)
        self.assertEqual(outcome["restored"], 0)
        self.assertFalse(source.exists())
        self.assertIn(
            "回收站中的文件已不存在",
            "\n".join(message for message, _ in logs),
        )
        self.assertEqual(service.state, "idle")

    # j) 空子目录阻止清理；非“重复文件”路径绝不 rmdir
    def test_j_cleanup_guards_subdirs_and_fake_paths(self) -> None:
        trash = self.folder / TRASH_DIR_NAME
        trash.mkdir()
        empty_sub = trash / "only_empty"
        empty_sub.mkdir()
        service = scan_service.ScanService()
        self.assertEqual(
            service.cleanup_empty_trash(self.folder, trash, lambda *_a: None),
            "kept",
        )
        self.assertTrue(empty_sub.is_dir(), "空子目录被误删")

        fake_trash = self.folder / "备份"
        fake_trash.mkdir()
        calls: list[str] = []
        with mock.patch.object(
            scan_service.os, "rmdir", side_effect=lambda path: calls.append(str(path))
        ):
            self.assertEqual(
                service.cleanup_empty_trash(self.folder, fake_trash, lambda *_a: None),
                "invalid",
            )
        self.assertEqual(calls, [], "对非“重复文件”目录执行了 rmdir")
        self.assertTrue(fake_trash.is_dir())

    # ---------- 整组丢弃 ----------
    def _multi_group(self, names: list[str], folder: str = "") -> DuplicateGroup:
        infos = []
        for name in names:
            rel = Path(folder) / name if folder else Path(name)
            path = self.folder / rel
            infos.append(
                ImageInfo(
                    path=path,
                    rel_path=rel,
                    file_size=path.stat().st_size if path.is_file() else 1,
                )
            )
        return DuplicateGroup(kind="exact", keeper=infos[0], duplicates=infos[1:])

    # l) 整组丢弃：无 keeper 也能移动全部文件
    def test_l_discard_all_moves_every_file(self) -> None:
        names = ["a.png", "b.png", "c.png"]
        contents = {}
        for name in names:
            path = self.folder / name
            path.write_bytes(f"content-{name}".encode("utf-8"))
            contents[name] = path.read_bytes()
        group = self._multi_group(names)
        service = scan_service.ScanService()
        service.folder = self.folder

        # 明确传 keeper=None：整组丢弃不依赖保留项。
        outcome = service.apply_decision(
            group, "discard_all", None, root=self.folder
        )
        self.assertTrue(outcome.discarded)
        self.assertFalse(outcome.suspended)
        self.assertEqual(len(outcome.moves), 3)
        self.assertTrue(all(result.ok for result in outcome.moves))
        for name in names:
            self.assertFalse((self.folder / name).exists(), name)
            destination = self.folder / TRASH_DIR_NAME / name
            self.assertTrue(destination.is_file(), name)
            self.assertEqual(destination.read_bytes(), contents[name])
        self.assertEqual(len(service.undo_stack), 1)
        self.assertEqual(service.pending_groups, ())
        self.assertFalse(group.keeper.path.exists())

    # m) 整组丢弃后撤销：全部还原并清理空回收站
    def test_m_discard_all_undo_restores_all(self) -> None:
        names = ["a.png", "b.png", "c.png"]
        contents = {}
        for name in names:
            path = self.folder / name
            path.write_bytes(f"content-{name}".encode("utf-8"))
            contents[name] = path.read_bytes()
        group = self._multi_group(names)
        service = scan_service.ScanService()
        service.folder = self.folder
        service.apply_decision(group, "discard_all", None, root=self.folder)
        self.assertEqual(len(service.undo_stack), 1)

        outcome = self._undo_and_wait(service)
        self.assertEqual(outcome["restored"], 3)
        self.assertEqual(outcome["skipped"], 0)
        self.assertEqual(outcome["cleanup"], "removed")
        for name in names:
            path = self.folder / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(path.read_bytes(), contents[name])
        self.assertFalse((self.folder / TRASH_DIR_NAME).exists())
        self.assertFalse(service.has_undo)

    # n) 混合批次 LIFO：先普通决策后整组丢弃，撤销两次
    def test_n_mixed_batches_undo_lifo(self) -> None:
        for name in ("k1.png", "k2.png", "d1.png", "d2.png", "d3.png"):
            (self.folder / name).write_bytes(f"content-{name}".encode("utf-8"))
        first = self._multi_group(["k1.png", "k2.png"])
        second = self._multi_group(["d1.png", "d2.png", "d3.png"])
        service = scan_service.ScanService()
        service.folder = self.folder

        service.apply_decision(first, "confirm", first.keeper, root=self.folder)
        service.apply_decision(second, "discard_all", None, root=self.folder)
        self.assertEqual(len(service.undo_stack), 2)
        self.assertFalse((self.folder / "k2.png").exists())
        for name in ("d1.png", "d2.png", "d3.png"):
            self.assertFalse((self.folder / name).exists())

        # 第一次撤销只恢复后发生的整组丢弃。
        outcome = self._undo_and_wait(service)
        self.assertEqual(outcome["restored"], 3)
        self.assertEqual(outcome["cleanup"], "kept")
        for name in ("d1.png", "d2.png", "d3.png"):
            self.assertTrue((self.folder / name).is_file(), name)
        self.assertFalse((self.folder / "k2.png").exists(), "LIFO 顺序错误")
        self.assertEqual(len(service.undo_stack), 1)

        # 第二次撤销恢复普通决策移走的重复项。
        outcome = self._undo_and_wait(service)
        self.assertEqual(outcome["restored"], 1)
        self.assertEqual(outcome["cleanup"], "removed")
        self.assertTrue((self.folder / "k2.png").is_file())
        self.assertEqual(service.undo_stack, [])
        self.assertFalse((self.folder / TRASH_DIR_NAME).exists())

    # o) 子目录 + 撤销同名冲突：不覆盖用户新文件
    def test_o_discard_all_subdirs_and_conflict_undo(self) -> None:
        folder = "sub"
        names = ["a.png", "b.png"]
        contents = {}
        for name in names:
            path = self.folder / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"content-{name}".encode("utf-8"))
            contents[name] = path.read_bytes()
        group = self._multi_group(names, folder=folder)
        service = scan_service.ScanService()
        service.folder = self.folder
        outcome = service.apply_decision(
            group, "discard_all", None, root=self.folder
        )
        self.assertTrue(outcome.discarded)
        for name in names:
            destination = self.folder / TRASH_DIR_NAME / folder / name
            self.assertTrue(destination.is_file(), name)
            self.assertEqual(destination.read_bytes(), contents[name])

        # 撤销前用户在原路径新建同名文件：必须跳过且不覆盖。
        user_file = self.folder / folder / "a.png"
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_bytes(b"user-new-file")
        self.assertFalse((self.folder / folder / "b.png").exists())

        result = self._undo_and_wait(service)
        self.assertEqual(result["restored"], 1)
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["cleanup"], "kept")
        self.assertEqual(user_file.read_bytes(), b"user-new-file")
        self.assertTrue((self.folder / folder / "b.png").is_file())
        self.assertTrue(
            (self.folder / TRASH_DIR_NAME / folder / "a.png").is_file(),
            "冲突项不应从回收站消失",
        )

    # p) 单组撤销：保留移动后只还原该组，摘除后栈为空
    def test_p_undo_group_confirm_restores_files(self) -> None:
        for name in ("p1.png", "p2.png"):
            (self.folder / name).write_bytes(f"content-{name}".encode("utf-8"))
        group = self._multi_group(["p1.png", "p2.png"])
        service = scan_service.ScanService()
        service.folder = self.folder
        service.apply_decision(group, "confirm", group.keeper, root=self.folder)
        self.assertTrue((self.folder / TRASH_DIR_NAME / "p2.png").is_file())
        self.assertEqual(len(service.undo_stack), 1)

        result = service.undo_group(group)
        self.assertTrue(result.ok)
        self.assertEqual(result.restored, 1)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.cleanup, "removed")
        self.assertTrue((self.folder / "p2.png").is_file())
        self.assertFalse((self.folder / TRASH_DIR_NAME).exists())
        self.assertFalse(service.has_undo)
        self.assertEqual(service.state, "idle")

        # 已撤销的组再次撤销：明确返回 missing，不崩溃。
        again = service.undo_group(group)
        self.assertFalse(again.ok)
        self.assertEqual(again.error, "missing")

    # q) 整组丢弃后单组撤销：全部还原
    def test_q_undo_group_discard_all_restores_every_file(self) -> None:
        names = ["q1.png", "q2.png", "q3.png"]
        contents = {}
        for name in names:
            path = self.folder / name
            path.write_bytes(f"content-{name}".encode("utf-8"))
            contents[name] = path.read_bytes()
        group = self._multi_group(names)
        service = scan_service.ScanService()
        service.folder = self.folder
        service.apply_decision(group, "discard_all", None, root=self.folder)
        self.assertTrue((self.folder / TRASH_DIR_NAME).is_dir())

        result = service.undo_group(group)
        self.assertTrue(result.ok)
        self.assertEqual(result.restored, 3)
        self.assertEqual(result.skipped, 0)
        self.assertEqual(result.cleanup, "removed")
        for name in names:
            path = self.folder / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(path.read_bytes(), contents[name])
        self.assertFalse((self.folder / TRASH_DIR_NAME).exists())
        self.assertFalse(service.has_undo)

    # r) 隔离性：撤销 B 组不影响 A 组，随后全量撤销仍能还原 A
    def test_r_undo_group_isolation_keeps_other_batches(self) -> None:
        for name in ("r1.png", "r2.png", "r3.png", "r4.png"):
            (self.folder / name).write_bytes(f"content-{name}".encode("utf-8"))
        first = self._multi_group(["r1.png", "r2.png"])
        second = self._multi_group(["r3.png", "r4.png"])
        service = scan_service.ScanService()
        service.folder = self.folder
        service.apply_decision(first, "confirm", first.keeper, root=self.folder)
        service.apply_decision(second, "confirm", second.keeper, root=self.folder)
        self.assertEqual(len(service.undo_stack), 2)

        result = service.undo_group(second)
        self.assertTrue(result.ok)
        self.assertEqual(result.restored, 1)
        self.assertTrue((self.folder / "r4.png").is_file())
        # A 组的移动不受影响，仍留在回收站。
        self.assertTrue((self.folder / TRASH_DIR_NAME / "r2.png").is_file())
        self.assertFalse((self.folder / "r2.png").exists())
        self.assertEqual(len(service.undo_stack), 1)

        # 全局撤销仍按剩余批次正确还原 A 组。
        outcome = self._undo_and_wait(service)
        self.assertEqual(outcome["restored"], 1)
        self.assertEqual(outcome["cleanup"], "removed")
        self.assertTrue((self.folder / "r2.png").is_file())
        self.assertFalse(service.has_undo)
        self.assertFalse((self.folder / TRASH_DIR_NAME).exists())

    # s) 非栈顶批次撤销：先 A 后 B，撤销 A 后只剩 B，全量撤销 B 正常
    def test_s_undo_non_top_batch_then_undo_rest(self) -> None:
        for name in ("s1.png", "s2.png", "s3.png", "s4.png"):
            (self.folder / name).write_bytes(f"content-{name}".encode("utf-8"))
        first = self._multi_group(["s1.png", "s2.png"])
        second = self._multi_group(["s3.png", "s4.png"])
        service = scan_service.ScanService()
        service.folder = self.folder
        service.apply_decision(first, "confirm", first.keeper, root=self.folder)
        service.apply_decision(second, "discard_all", None, root=self.folder)
        self.assertEqual(len(service.undo_stack), 2)
        self.assertFalse((self.folder / "s2.png").exists())
        self.assertFalse((self.folder / "s3.png").exists())

        # A 批次不在栈顶：精确摘除，不应只弹栈顶。
        result = service.undo_group(first)
        self.assertTrue(result.ok)
        self.assertEqual(result.restored, 1)
        self.assertTrue((self.folder / "s2.png").is_file())
        self.assertFalse((self.folder / "s3.png").exists(), "B 组被误撤销")
        stack = service.undo_stack
        self.assertEqual(len(stack), 1)
        self.assertIn((self.folder / "s3.png", self.folder / TRASH_DIR_NAME / "s3.png"), stack[0])
        self.assertIn((self.folder / "s4.png", self.folder / TRASH_DIR_NAME / "s4.png"), stack[0])

        # 剩余 B 批次仍可用全量撤销恢复，且 A 已还原的文件保持原样。
        outcome = self._undo_and_wait(service)
        self.assertEqual(outcome["restored"], 2)
        for name in ("s1.png", "s2.png", "s3.png", "s4.png"):
            self.assertTrue((self.folder / name).is_file(), name)
        self.assertFalse(service.has_undo)
        self.assertFalse((self.folder / TRASH_DIR_NAME).exists())

    # t) 子目录 + 同名冲突：单组撤销不覆盖用户新文件
    def test_t_undo_group_subdir_conflict_never_overwrites(self) -> None:
        folder = "sub"
        for name in ("t1.png", "t2.png"):
            path = self.folder / folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(f"content-{name}".encode("utf-8"))
        group = self._multi_group(["t1.png", "t2.png"], folder=folder)
        service = scan_service.ScanService()
        service.folder = self.folder
        service.apply_decision(group, "discard_all", None, root=self.folder)

        user_file = self.folder / folder / "t1.png"
        user_file.parent.mkdir(parents=True, exist_ok=True)
        user_file.write_bytes(b"user-new-file")

        result = service.undo_group(group)
        self.assertTrue(result.ok)
        self.assertEqual(result.restored, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.cleanup, "kept")
        self.assertEqual(user_file.read_bytes(), b"user-new-file")
        self.assertTrue((self.folder / folder / "t2.png").is_file())
        self.assertTrue(
            (self.folder / TRASH_DIR_NAME / folder / "t1.png").is_file(),
            "冲突项不应从回收站消失",
        )
        self.assertFalse(service.has_undo)

    # u) 批处理模式：撤销后该组可重新决策，统计与决策槽一并回退
    def test_u_undo_group_allows_redecision_in_batch_mode(self) -> None:
        for name in ("u1.png", "u2.png", "u3.png", "u4.png"):
            (self.folder / name).write_bytes(f"content-{name}".encode("utf-8"))
        groups = [
            self._multi_group(["u1.png", "u2.png"]),
            self._multi_group(["u3.png", "u4.png"]),
        ]
        report = ScanReport(root=self.folder, scanned=4, exact_groups=groups)
        delivered: list = []
        ready = threading.Event()
        summaries: list = []
        service = scan_service.ScanService(
            groups_ready_cb=lambda g, r: (delivered.extend(g), ready.set()),
            review_finished_cb=lambda summary: summaries.append(summary),
            review_mode=scan_service.REVIEW_MODE_BATCH,
        )
        with mock.patch.object(scan_service, "scan_folder", return_value=report):
            self.assertTrue(service.start_scan(self.folder, 8))
            self.assertTrue(ready.wait(5.0), "未一次性交出全部组")
        self.assertEqual(service.state, "reviewing")
        first, second = delivered

        service.apply_decision(first, "confirm", first.keeper, root=self.folder)
        self.assertTrue((self.folder / TRASH_DIR_NAME / "u2.png").is_file())

        # 复核仍在进行（reviewing）时也允许撤销已处理组。
        result = service.undo_group(first)
        self.assertTrue(result.ok)
        self.assertEqual(result.restored, 1)
        self.assertTrue((self.folder / "u2.png").is_file())
        self.assertFalse(service.has_undo)
        self.assertEqual(service.state, "reviewing")

        # 决策槽已回退：再次决策不会被当作重复而忽略。
        again = service.apply_decision(first, "confirm", first.keeper, root=self.folder)
        self.assertFalse(again.duplicate)
        self.assertEqual(len(again.moves), 1)
        self.assertTrue(again.moves[0].ok)
        self.assertEqual(service.apply_decision(second, "confirm", second.keeper, root=self.folder).duplicate, False)
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0].confirmed_groups, 2)
        self.assertEqual(summaries[0].moved_files, 2)
        self.assertEqual(len(service.undo_stack), 2)
        self.assertEqual(service.state, "idle")

    def _undo_all_and_wait(self, service: scan_service.ScanService) -> dict:
        """调用全量撤销并等待 5 参数完成回调。"""
        service._folder = self.folder
        result: dict = {}
        done = threading.Event()

        def on_undo(items, restored, skipped, cleanup, failures=None):
            result.update(
                items=list(items),
                restored=restored,
                skipped=skipped,
                cleanup=cleanup,
                failures=list(failures or []),
            )
            done.set()

        service._undo_finished_cb = on_undo
        self.assertTrue(service.undo_all_moves())
        self._wait(done)
        return result

    # v) 多批次（含整组丢弃）一键全量撤销：全部还原、清空栈、删空回收站
    def test_v_undo_all_moves_multi_batches(self) -> None:
        for name in ("v1.png", "v2.png", "v3.png", "v4.png", "v5.png", "v6.png", "v7.png"):
            (self.folder / name).write_bytes(f"content-{name}".encode("utf-8"))
        first = self._multi_group(["v1.png", "v2.png"])
        second = self._multi_group(["v3.png", "v4.png", "v5.png"])
        third = self._multi_group(["v6.png", "v7.png"])
        service = scan_service.ScanService()
        service.folder = self.folder
        service.apply_decision(first, "confirm", first.keeper, root=self.folder)
        service.apply_decision(second, "discard_all", None, root=self.folder)
        service.apply_decision(third, "confirm", third.keeper, root=self.folder)
        self.assertEqual(len(service.undo_stack), 3)
        self.assertEqual(service.state, "idle")

        outcome = self._undo_all_and_wait(service)
        self.assertEqual(outcome["restored"], 5)
        self.assertEqual(outcome["skipped"], 0)
        self.assertEqual(outcome["failures"], [])
        self.assertEqual(outcome["cleanup"], "removed")
        for name in (
            "v1.png", "v2.png", "v3.png", "v4.png",
            "v5.png", "v6.png", "v7.png",
        ):
            path = self.folder / name
            self.assertTrue(path.is_file(), name)
            self.assertEqual(path.read_bytes(), f"content-{name}".encode("utf-8"))
        self.assertFalse((self.folder / TRASH_DIR_NAME).exists())
        self.assertFalse(service.has_undo)
        self.assertEqual(service.undo_stack, [])
        self.assertEqual(service.pending_groups, ())

    # w) 部分失败：缺失/同名只记录警告并跳过，其余文件仍继续还原
    def test_w_undo_all_moves_partial_failure_continues(self) -> None:
        for name in ("w1.png", "w2.png", "w3.png", "w4.png"):
            (self.folder / name).write_bytes(f"content-{name}".encode("utf-8"))
        first = self._multi_group(["w1.png", "w2.png"])
        second = self._multi_group(["w3.png", "w4.png"])
        logs: list[tuple[str, str]] = []
        service = scan_service.ScanService(
            log_cb=lambda message, kind: logs.append((message, kind))
        )
        service.folder = self.folder
        service.apply_decision(first, "discard_all", None, root=self.folder)
        service.apply_decision(second, "discard_all", None, root=self.folder)

        # 回收站中的 w2 已被外部删除；w3 原位置被用户同名文件占用。
        (self.folder / TRASH_DIR_NAME / "w2.png").unlink()
        (self.folder / "w3.png").write_bytes(b"user-new-file")

        outcome = self._undo_all_and_wait(service)
        self.assertEqual(outcome["restored"], 2)
        self.assertEqual(outcome["skipped"], 2)
        self.assertIn("回收站文件缺失", outcome["failures"])
        self.assertIn("原位置同名未覆盖", outcome["failures"])
        self.assertEqual(outcome["cleanup"], "kept")
        self.assertTrue((self.folder / "w1.png").is_file())
        self.assertTrue((self.folder / "w4.png").is_file())
        self.assertEqual((self.folder / "w3.png").read_bytes(), b"user-new-file")
        self.assertFalse((self.folder / "w2.png").exists())
        joined = "\n".join(message for message, _ in logs)
        self.assertIn("回收站中的文件已不存在", joined)
        self.assertIn("未覆盖", joined)
        self.assertFalse(service.has_undo)
        self.assertEqual(service.state, "idle")

    # x) 工作台“撤销本组”摘走部分批次后，全量撤销只处理剩余批次
    def test_x_undo_all_moves_after_group_undo(self) -> None:
        for name in ("x1.png", "x2.png", "x3.png", "x4.png"):
            (self.folder / name).write_bytes(f"content-{name}".encode("utf-8"))
        first = self._multi_group(["x1.png", "x2.png"])
        second = self._multi_group(["x3.png", "x4.png"])
        service = scan_service.ScanService()
        service.folder = self.folder
        service.apply_decision(first, "discard_all", None, root=self.folder)
        service.apply_decision(second, "discard_all", None, root=self.folder)
        self.assertEqual(len(service.undo_stack), 2)

        result = service.undo_group(first)
        self.assertTrue(result.ok)
        self.assertEqual(result.restored, 2)
        self.assertTrue((self.folder / "x1.png").is_file())
        self.assertTrue((self.folder / "x2.png").is_file())
        self.assertEqual(len(service.undo_stack), 1)

        outcome = self._undo_all_and_wait(service)
        self.assertEqual(outcome["restored"], 2)
        self.assertEqual(outcome["cleanup"], "removed")
        for name in ("x1.png", "x2.png", "x3.png", "x4.png"):
            self.assertTrue((self.folder / name).is_file(), name)
        self.assertFalse(service.has_undo)
        self.assertFalse((self.folder / TRASH_DIR_NAME).exists())

    # y) 全量撤销后再次扫描同一目录：无残留状态，扫描正常完成
    def test_y_rescan_after_undo_all(self) -> None:
        for name in ("y1.png", "y2.png"):
            (self.folder / name).write_bytes(f"content-{name}".encode("utf-8"))
        group = self._multi_group(["y1.png", "y2.png"])
        finished: list[ScanReport] = []
        done = threading.Event()
        service = scan_service.ScanService(
            finished_cb=lambda report: (finished.append(report), done.set())
        )
        service.folder = self.folder
        service.apply_decision(group, "discard_all", None, root=self.folder)
        service.add_suspended(group, 1, 1, self.folder)
        self.assertTrue(service.pending_groups)

        self._undo_all_and_wait(service)
        self.assertEqual(service.state, "idle")
        self.assertEqual(service.pending_groups, ())
        self.assertFalse(service.has_undo)

        empty = ScanReport(root=self.folder, scanned=2)
        with mock.patch.object(scan_service, "scan_folder", return_value=empty):
            self.assertTrue(service.start_scan(self.folder, 8))
            self._wait(done)
            deadline = time.perf_counter() + 1.0
            while service.state != "idle" and time.perf_counter() < deadline:
                time.sleep(0.01)
        self.assertEqual(len(finished), 1)
        self.assertEqual(service.state, "idle")

    # k) 只移动不删除：源码层删除语义白名单
    def test_k_delete_api_allowlist(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        sources = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(project_root.glob("*.py"))
        }
        combined = "\n".join(sources.values())
        for token in (
            "shutil.rmtree",
            "os.remove",
            "os.unlink",
            "send2trash",
        ):
            self.assertNotIn(token, combined, f"出现禁止的删除语义：{token}")
        for name in (
            "main.py",
            "scan_service.py",
            "ui_qt_main.py",
            "review_qt.py",
            "review_workbench_qt.py",
            "platform_support.py",
        ):
            self.assertNotIn("os.walk", sources[name], f"UI 侧禁止递归遍历：{name}")
        # 允许且仅允许 1 处 os.rmdir，位于撤销清理函数内。
        self.assertEqual(combined.count("os.rmdir("), 1)
        cleanup_source = inspect.getsource(scan_service.ScanService.cleanup_empty_trash)
        self.assertIn("os.listdir", cleanup_source)
        self.assertIn("os.rmdir", cleanup_source)
        self.assertIn("TRASH_DIR_NAME", cleanup_source)
        self.assertNotIn("rmtree", cleanup_source)
        self.assertNotIn("os.walk", cleanup_source)


if __name__ == "__main__":
    unittest.main()
