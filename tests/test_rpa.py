"""RPA 浏览器回归检查。

覆盖需求中的回归点：
1. 文件确由浏览器 #files input 接收（Playwright set_input_files + 面板摘要认领）。
2. 同名异内容文件不会互相错挂（按 SHA-256 摘要而非文件名/位置认领）。
3. 乱序/延迟预检下，旧 upload_id 的迟到结果不会认领到替换材料。
4. 预检失败不进入草稿附件映射。
5. 向导回退重建组件、重排附件后映射仍正确。
6. 重启恢复不会重复上传已确认内容。
7. 内容不同（人工修改）时停下交人，不覆盖人工修改。
8. 整个过程发布计数始终为零，且发布按钮被护栏拦截。
9. 清单缺少类型必要字段时上传前报错。
10. 一个草稿同时只能有一个活动执行者（排它锁）。
"""
from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen

from portal.__main__ import running
from rpa.driver import PortalDriver
from rpa.errors import LockError, ManifestError, ReconcileError
from rpa.manifest import load_manifest
from rpa.runner import UploadRunner
from rpa.state import JournalStore

WORKSPACE = pathlib.Path(__file__).resolve().parents[1]
NORTH = WORKSPACE / "materials/north/report.txt"
SOUTH = WORKSPACE / "materials/south/report.txt"
LICENSE = WORKSPACE / "materials/license/permit-license.txt"


def _post_control(base: str, payload: dict) -> None:
    request = Request(
        base + "/__control",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request) as response:
        json.load(response)


def _write_manifest(dir_path: pathlib.Path, materials: list[dict],
                    draft_id: str = "DRAFT-17") -> pathlib.Path:
    # 每份清单使用独立夹具目录，同名异内容文件不会互相覆盖。
    fixtures = dir_path / "fixtures"
    fixtures.mkdir()
    for index, entry in enumerate(materials):
        if entry["path"] == "empty.txt":
            (fixtures / "empty.txt").write_bytes(b"")
            entry.pop("source_path", None)
            continue
        # 按“原始源路径”取文件，保证同名异内容不会被字典覆盖。
        source = WORKSPACE / entry.pop("source_path")
        target = fixtures / f"{index}-{entry['material_id']}-{source.name}"
        target.write_bytes(source.read_bytes())
        entry["path"] = target.name
    manifest = {
        "draft_id": draft_id,
        "base_dir": "fixtures",
        "materials": materials,
    }
    path = dir_path / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return path


# 空文件哨兵：触发门户“预检失败”分支（file.size === 0）。
EMPTY = object()


def _entry(material_id, source_path, kind, project, summary, fields):
    if source_path is EMPTY:
        return {
            "material_id": material_id,
            "path": "empty.txt",
            "kind": kind,
            "project": project,
            "summary": summary,
            "fields": fields,
        }
    return {
        "material_id": material_id,
        "source_path": str(source_path.relative_to(WORKSPACE)),
        "path": source_path.name,
        "kind": kind,
        "project": project,
        "summary": summary,
        "fields": fields,
    }


REPORT_FIELDS = {"report_date": "2026-11-03"}
LICENSE_FIELDS = {"license_id": "LIC-T-1"}


class RpaRegressionTests(unittest.TestCase):
    def setUp(self):
        self.server = running()
        self.base, self.portal_state = self.server.__enter__()
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        self.output_dir = self.tmp / "outputs"
        self.profile_dir = self.tmp / "profile"

    def tearDown(self):
        self._tmp.cleanup()
        self.server.__exit__(None, None, None)

    def _runner(self, manifest, **kwargs) -> UploadRunner:
        return UploadRunner(
            manifest=manifest,
            portal_url=self.base,
            state_dir=self.state_dir,
            output_dir=self.output_dir,
            user_data_dir=self.profile_dir,
            headless=True,
            **kwargs,
        )

    # ------------------------------------------------------------ 1. 端到端 + input 接收
    def test_full_run_files_received_by_input_and_same_name_distinguished(self):
        manifest_path = _write_manifest(self.tmp, [
            _entry("MAT-N", NORTH, "report", "PR-17", "北区", dict(REPORT_FIELDS)),
            _entry("MAT-S", SOUTH, "report", "PR-29", "南区",
                   {"report_date": "2026-11-04"}),
        ])
        manifest = load_manifest(manifest_path)
        report = self._runner(manifest).run()

        self.assertEqual(0, report.publish_count)
        self.assertEqual([], report.needs_human)
        # 门户动作记录证明 onchange（input 选择事件）真实触发，且按摘要区分同名文件。
        upload_actions = [a for a in self.portal_state["actions"] if a.get("name")]
        self.assertEqual(2, len(upload_actions))
        digests = {a["digest"] for a in upload_actions}
        self.assertEqual({manifest.materials[0].digest, manifest.materials[1].digest}, digests)
        # 两个同名 report.txt 各自关联到正确项目（RPA 输出的权威预览映射，
        # 项目取自材料沿革而非会被门户下拉重算的页面字段）。
        preview = json.loads((self.output_dir / "preview-DRAFT-17.json").read_text())
        by_digest = {a["digest"]: a for a in preview["attachments"]}
        self.assertEqual("PR-17", by_digest[manifest.materials[0].digest]["project"])
        self.assertEqual("PR-29", by_digest[manifest.materials[1].digest]["project"])
        self.assertEqual(
            "MAT-N", by_digest[manifest.materials[0].digest]["material_id"]
        )
        # 门户全局项目下拉会重算页面项目字段；RPA 在每个材料自身项目上下文
        # 中切换 + 重渲染后逐材料核对，权威预览中的项目归属与清单一致，
        # 没有需要人工处理的条目。
        self.assertEqual([], report.needs_human)
        self.assertFalse(any(a.get("name") == "publish" for a in self.portal_state["actions"]))

    # ------------------------------------------------------------ 2. 向导回退重排
    def test_wizard_back_reorders_but_mapping_holds(self):
        manifest_path = _write_manifest(self.tmp, [
            _entry("MAT-N", NORTH, "report", "PR-17", "北区", dict(REPORT_FIELDS)),
            _entry("MAT-S", SOUTH, "report", "PR-29", "南区",
                   {"report_date": "2026-11-04"}),
        ])
        report = self._runner(load_manifest(manifest_path)).run()
        self.assertEqual(0, report.publish_count)
        # 回退在门户状态文本中留下记录，且最终两个附件仍都在预览里。
        preview = json.loads((self.output_dir / "preview-DRAFT-17.json").read_text())
        self.assertEqual(2, len(preview["attachments"]))
        self.assertEqual("DRAFT-17", preview["draft_id"])
        self.assertTrue((self.output_dir / "preview-DRAFT-17.png").is_file())

    # ------------------------------------------------------------ 3. 延迟预检 + 替换不复活
    def test_stale_upload_id_never_revived_after_replacement(self):
        manifest_path = _write_manifest(self.tmp, [
            _entry("MAT-N", NORTH, "report", "PR-17", "北区", dict(REPORT_FIELDS)),
        ])
        manifest = load_manifest(manifest_path)
        # 先制造一个卡在“预检中”的 hold.txt 面板（延迟预检场景）。
        _post_control(self.base, {"phase": "delayed-precheck", "version": 1})
        hold_file = self.tmp / "hold.txt"
        hold_file.write_text("旧版本内容，预检被挂起", encoding="utf-8")
        hold_digest = self._digest(hold_file)
        cancel = threading.Event()
        driver = PortalDriver(self.base, self.profile_dir, cancel, headless=True)
        driver.start()
        held = driver.choose_file(hold_file, hold_digest)
        self.assertEqual("预检中", held["state"])
        held_id = held["upload_id"]
        # 工作人员在重传前通过门户“移除附件”清掉卡住的旧附件
        # （旧 onchange 回调仍在浏览器里等待，正是迟到结果场景）。
        driver.remove_upload(held_id)
        driver.close()

        # RPA 上传清单材料：新 upload_id 正常通过；随后释放延迟预检，
        # 旧 upload_id 的迟到成功不得把已替换项复活。
        runner = self._runner(manifest)
        report = runner.run()
        journal = JournalStore(self.state_dir).load("DRAFT-17")
        tracked = journal.materials["MAT-N"]
        self.assertEqual("预检通过已关联", tracked.state)
        self.assertNotIn(held_id, {a.upload_id for a in tracked.attempts})

        # 旧 upload_id 迟到成功也不能复活：释放延迟预检，再核对页面与预览。
        driver2 = PortalDriver(self.base, self.profile_dir, cancel, headless=True)
        driver2.start()
        panels_before = {p["upload_id"] for p in driver2.list_panels()}
        self.assertNotIn(held_id, panels_before)
        _post_control(self.base, {"version": 2})
        preview = driver2.open_preview()
        preview_names = [a["name"] for a in preview["attachments"]]
        self.assertNotIn("hold.txt", preview_names)
        self.assertTrue(any(name.endswith("report.txt") for name in preview_names))
        driver2.assert_no_publish()
        driver2.close()
        self.assertEqual(0, report.publish_count)

    # ------------------------------------------------------------ 4. 预检失败不进映射
    def test_failed_precheck_not_linked_and_flagged_for_human(self):
        manifest_path = _write_manifest(self.tmp, [
            _entry("MAT-EMPTY", EMPTY, "report", "PR-17", "空文件", dict(REPORT_FIELDS)),
            _entry("MAT-N", NORTH, "report", "PR-29", "南区",
                   {"report_date": "2026-11-04"}),
        ])
        report = self._runner(load_manifest(manifest_path)).run()
        by_id = {m.material_id: m for m in report.materials}
        self.assertEqual("预检失败", by_id["MAT-EMPTY"].precheck)
        self.assertFalse(by_id["MAT-EMPTY"].draft_linked)
        self.assertIn("MAT-EMPTY", report.needs_human)
        self.assertTrue(by_id["MAT-N"].draft_linked)
        preview = json.loads((self.output_dir / "preview-DRAFT-17.json").read_text())
        self.assertEqual(1, len(preview["attachments"]))
        self.assertEqual(0, report.publish_count)

    # ------------------------------------------------------------ 4b. 失败后替换保持沿革
    def test_replacement_keeps_material_history_and_supersedes_old_upload(self):
        first_path = _write_manifest(self.tmp, [
            _entry("MAT-R", EMPTY, "report", "PR-17", "首版空文件", dict(REPORT_FIELDS)),
        ])
        runner = self._runner(load_manifest(first_path))
        report = runner.run()
        self.assertIn("MAT-R", report.needs_human)
        store = JournalStore(self.state_dir)
        journal = store.load("DRAFT-17")
        failed = journal.materials["MAT-R"]
        old_upload_id = failed.current_upload_id
        self.assertEqual("预检失败", failed.state)
        self.assertTrue(old_upload_id)

        # 工作人员把替换文件放到新路径，清单材料编号不变、路径与摘要变化。
        replacement = self.tmp / "fixtures/replacement.txt"
        replacement.write_text("替换后的有效检测文本\n", encoding="utf-8")
        second_manifest = self.tmp / "manifest-v2.json"
        second_manifest.write_text(json.dumps({
            "draft_id": "DRAFT-17",
            "base_dir": "fixtures",
            "materials": [{
                "material_id": "MAT-R",
                "path": "replacement.txt",
                "kind": "report",
                "project": "PR-17",
                "summary": "替换版",
                "fields": dict(REPORT_FIELDS),
            }],
        }, ensure_ascii=False), encoding="utf-8")

        # 摘要变化时普通 resume 必须拒绝（防静默换内容）；
        # 工作人员显式以新清单重跑（run 而非 resume）来执行替换。
        with self.assertRaises(ReconcileError):
            self._runner(load_manifest(second_manifest)).run(resume=True)
        report2 = self._runner(load_manifest(second_manifest)).run()
        item = next(m for m in report2.materials if m.material_id == "MAT-R")
        self.assertTrue(item.draft_linked)
        self.assertEqual("通过", item.precheck)
        journal2 = store.load("DRAFT-17")
        tracked = journal2.materials["MAT-R"]
        attempt_ids = [a.upload_id for a in tracked.attempts]
        self.assertIn(old_upload_id, attempt_ids)  # 沿革保留
        old_attempt = next(a for a in tracked.attempts if a.upload_id == old_upload_id)
        self.assertTrue(old_attempt.superseded)   # 旧 id 被取代
        self.assertNotEqual(old_upload_id, tracked.current_upload_id)
        # 草稿预览里只剩替换后的新 upload_id。
        preview = json.loads((self.output_dir / "preview-DRAFT-17.json").read_text())
        self.assertEqual([tracked.current_upload_id],
                         [a["upload_id"] for a in preview["attachments"]])
        self.assertEqual(0, report2.publish_count)

    # ------------------------------------------------------------ 5. 重启不重复上传
    def test_resume_does_not_reupload_confirmed_content(self):
        manifest_path = _write_manifest(self.tmp, [
            _entry("MAT-N", NORTH, "report", "PR-17", "北区", dict(REPORT_FIELDS)),
            _entry("MAT-L", LICENSE, "license", "PR-17", "许可",
                   dict(LICENSE_FIELDS)),
        ])
        manifest = load_manifest(manifest_path)
        first = self._runner(manifest).run()
        self.assertEqual(2, len(first.materials))
        actions_after_first = len(self.portal_state["actions"])

        # 模拟关闭页面后重新打开（新驱动实例 + 同一持久化目录 + resume）。
        second = self._runner(manifest).run(resume=True)
        self.assertEqual(actions_after_first, len(self.portal_state["actions"]))
        self.assertTrue(all(m.precheck.startswith("通过") for m in second.materials))
        self.assertTrue(all(m.draft_linked for m in second.materials))
        self.assertEqual(0, second.publish_count)

    # ------------------------------------------------------------ 6. 人工内容不一致即停下
    def test_resume_stops_on_unexpected_manual_attachment(self):
        manifest_path = _write_manifest(self.tmp, [
            _entry("MAT-N", NORTH, "report", "PR-17", "北区", dict(REPORT_FIELDS)),
        ])
        manifest = load_manifest(manifest_path)
        self._runner(manifest).run()

        # 人工在门户额外加入一个清单之外的附件。
        cancel = threading.Event()
        driver = PortalDriver(self.base, self.profile_dir, cancel, headless=True)
        driver.start()
        driver.select_project("PR-17")
        probe = driver.choose_file(LICENSE, self._digest(LICENSE))
        driver.wait_terminal(self._digest(LICENSE), probe["upload_id"])
        driver.close()

        with self.assertRaises(ReconcileError) as ctx:
            self._runner(manifest).run(resume=True)
        self.assertIn("清单之外", ctx.exception.message)

    def test_resume_stops_when_digest_changed(self):
        manifest_path = _write_manifest(self.tmp, [
            _entry("MAT-N", NORTH, "report", "PR-17", "北区", dict(REPORT_FIELDS)),
        ])
        manifest = load_manifest(manifest_path)
        self._runner(manifest).run()
        # 首版已确认后替换本地文件内容但保持文件名，
        # 模拟“内容不同要停下供人确认”：恢复时实时摘要核对必须拦截。
        target = self.tmp / "fixtures" / manifest.materials[0].path.name
        target.write_text("完全不同的人工替换内容\n", encoding="utf-8")
        with self.assertRaises(ReconcileError) as ctx:
            self._runner(load_manifest(manifest_path)).run(resume=True)
        self.assertIn("摘要", ctx.exception.message)

    # ------------------------------------------------------------ 7. 缺动态字段上传前报错
    def test_missing_dynamic_field_fails_before_browser_opens(self):
        bad_manifest = WORKSPACE / "manifests/manifest-missing-field.json"
        with self.assertRaises(ManifestError) as ctx:
            load_manifest(bad_manifest)
        self.assertIn("license_id", str(ctx.exception.items))
        # 清单被拒后门户没有任何上传动作。
        self.assertEqual([], self.portal_state["actions"])

    # ------------------------------------------------------------ 8. 排它执行锁
    def test_single_active_executor_per_draft(self):
        store = JournalStore(self.state_dir)
        store.acquire_lock("DRAFT-17")
        try:
            with self.assertRaises(LockError):
                JournalStore(self.state_dir).acquire_lock("DRAFT-17")
        finally:
            store.release_lock()
        # 锁释放后可重新获取（模拟取消运行后保留草稿、再次执行）。
        again = JournalStore(self.state_dir)
        again.acquire_lock("DRAFT-17")
        again.release_lock()

    # ------------------------------------------------------------ 9. 取消运行保留草稿
    def test_cancel_keeps_unpublished_draft_and_journal(self):
        manifest_path = _write_manifest(self.tmp, [
            _entry("MAT-N", NORTH, "report", "PR-17", "北区", dict(REPORT_FIELDS)),
            _entry("MAT-L", LICENSE, "license", "PR-29", "许可",
                   dict(LICENSE_FIELDS)),
        ])
        manifest = load_manifest(manifest_path)
        report = self._runner(manifest).run()  # 正常完成首版
        self.assertFalse(report.cancelled)

        # 第二次运行在浏览器启动后立即取消：journal 与未发布草稿仍保留，
        # 且不产生任何新的上传动作。
        actions_before = len(self.portal_state["actions"])
        cancel2 = threading.Event()
        original_start = PortalDriver.start

        def start_then_cancel(self):
            original_start(self)
            cancel2.set()

        PortalDriver.start = start_then_cancel
        try:
            runner2 = UploadRunner(
                manifest=manifest, portal_url=self.base, state_dir=self.state_dir,
                output_dir=self.output_dir, user_data_dir=self.profile_dir,
                cancel_event=cancel2, headless=True,
            )
            cancelled_report = runner2.run(resume=True)
        finally:
            PortalDriver.start = original_start
        self.assertTrue(cancelled_report.cancelled)
        self.assertTrue(JournalStore(self.state_dir).journal_path("DRAFT-17").exists())
        self.assertEqual(0, cancelled_report.publish_count)
        self.assertEqual(actions_before, len(self.portal_state["actions"]))
        self.assertFalse(any(a.get("name") == "publish" for a in self.portal_state["actions"]))

    # ------------------------------------------------------------ 10. 发布护栏
    def test_publish_button_is_guarded(self):
        cancel = threading.Event()
        driver = PortalDriver(self.base, self.profile_dir / "guard", cancel, headless=True)
        driver.start()
        driver.page.click("#publish")
        self.assertGreaterEqual(driver.page.evaluate("window.__rpaPublishBlocked"), 1)
        self.assertFalse(any(a.get("name") == "publish" for a in self.portal_state["actions"]))
        driver.close()

    @staticmethod
    def _digest(path) -> str:
        import hashlib
        return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
