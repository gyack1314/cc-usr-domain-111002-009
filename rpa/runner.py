"""RPA 编排层：把清单逐条放入草稿、核对预检、维护编号沿革与恢复对账。

设计要点：
1. 每条材料上传前重新选择项目/类型/动态字段，回读核对后才操作 input。
2. 附件只凭 SHA-256 摘要 + upload_id 认领；同材料替换先移除旧 upload_id，
   并把它标记为 superseded，迟到成功也不会复活。
3. 预检失败不进入草稿附件映射（门户预览本身也只含“通过”项，RPA 再次核对）。
4. 每条材料之间触发向导回退，回退后重新核对 draft_id、项目与材料映射。
5. 恢复运行先打开同一草稿，用预览核对既有附件的摘要与归属；
   内容不同/存在无法解释的人工附件时停下交人工，绝不覆盖人工修改。
6. 全程不点击发布；驱动层另有拦截护栏。
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from .digestutil import sha256_file
from .driver import PRECHECK_FAILED, PRECHECK_PASSED, PortalDriver
from .errors import ReconcileError, RunCancelled
from .manifest import Manifest, Material
from .state import (
    Attempt,
    Journal,
    JournalStore,
    MaterialState,
    STATE_FAILED,
    STATE_NEEDS_HUMAN,
    STATE_PASSED,
    STATE_PENDING,
    STATE_REPLACED,
    STATE_SKIPPED_EXISTING,
    STATE_UPLOADED,
)


@dataclass
class MaterialReport:
    material_id: str
    project: str
    kind: str
    path: str
    digest: str
    state: str
    upload_id: str | None
    precheck: str
    field_validation: str
    draft_linked: bool
    note: str
    attempts: list[dict]


@dataclass
class RunReport:
    draft_id: str
    portal_url: str
    preview_url: str
    preview_path: str
    publish_count: int
    materials: list[MaterialReport]
    needs_human: list[str]
    cancelled: bool

    def to_dict(self) -> dict:
        return asdict(self)


class UploadRunner:
    def __init__(
        self,
        manifest: Manifest,
        portal_url: str,
        state_dir: str | Path,
        output_dir: str | Path,
        user_data_dir: str | Path,
        cancel_event: threading.Event | None = None,
        headless: bool = True,
        back_between_materials: bool = True,
    ):
        self.manifest = manifest
        self.portal_url = portal_url.rstrip("/")
        self.store = JournalStore(state_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.user_data_dir = user_data_dir
        self.cancel_event = cancel_event or threading.Event()
        self.headless = headless
        self.back_between_materials = back_between_materials
        self.journal: Journal | None = None

    # ------------------------------------------------------------------ journal
    def _build_fresh_journal(self) -> Journal:
        materials = {
            m.material_id: MaterialState(
                material_id=m.material_id,
                project=m.project,
                kind=m.kind,
                summary=m.summary,
                digest=m.digest,
                path=str(m.path),
                dynamic_fields=m.dynamic_fields,
            )
            for m in self.manifest.materials
        }
        return Journal(
            draft_id=self.manifest.draft_id,
            manifest_path=str(self.manifest.source),
            portal_url=self.portal_url,
            materials=materials,
        )

    def _save(self) -> None:
        assert self.journal is not None
        self.store.save(self.journal)

    # ------------------------------------------------------------------ 恢复对账
    def _reconcile_with_draft(self, driver: PortalDriver) -> None:
        """打开同一草稿，用预览核对既有附件；不一致即停下交人工。"""
        assert self.journal is not None
        page_draft_id = driver.read_draft_id()
        if page_draft_id != self.journal.draft_id:
            raise ReconcileError(
                "恢复时草稿编号不一致，停止运行等待人工确认：",
                items=[f"清单草稿 {self.journal.draft_id}", f"门户草稿 {page_draft_id}"],
            )
        self.journal.verified_draft_id = page_draft_id

        # 恢复前先重算本地文件摘要：清单生成后的任何内容变化都要停下交人，
        # 不能用旧摘要去认领新内容，也不能覆盖人工修改。
        for material in self.manifest.materials:
            live_digest = sha256_file(material.path)
            tracked = self.journal.materials[material.material_id]
            if live_digest != tracked.digest:
                raise ReconcileError(
                    "本地文件内容与已确认摘要不一致，停止并请人工确认：",
                    items=[
                        f"{material.material_id} 已确认 {tracked.digest[:12]}… / "
                        f"当前 {live_digest[:12]}…"
                    ],
                )

        # 门户预览的 project 字段受全局下拉重算，不作为归属权威；
        # 这里只比对通过附件的摘要集合与草稿编号。
        draft_id, preview_digests = driver.passed_preview_digests()
        if draft_id != self.journal.draft_id:
            raise ReconcileError(
                "预览返回的草稿编号与清单不符：", items=[draft_id]
            )

        journal_by_digest = {m.digest: m for m in self.journal.materials.values()}
        reconciled: set[str] = set()

        for digest in preview_digests:
            tracked = journal_by_digest.get(digest)
            if tracked is None:
                # 人工或其他执行者添加的内容：不覆盖、不删除，停下确认。
                raise ReconcileError(
                    "草稿存在清单之外的附件，可能为人工添加，停止并请人工确认：",
                    items=[f"未知摘要 {digest[:12]}…"],
                )
            # 内容摘要 + 所属材料一致，确认该材料已存在，重启后不重复上传。
            upload_id = next(
                (p["upload_id"] for p in driver.list_panels() if p["digest"] == digest),
                tracked.current_upload_id,
            )
            tracked.state = STATE_SKIPPED_EXISTING
            tracked.current_upload_id = upload_id
            if not any(a.upload_id == upload_id for a in tracked.attempts):
                tracked.attempts.append(
                    Attempt(
                        upload_id=upload_id,
                        digest=digest,
                        path=tracked.path,
                        state=PRECHECK_PASSED,
                    )
                )
            tracked.note = f"恢复确认：预览附件 {upload_id} 摘要与本地一致，归属 {tracked.project}"
            reconciled.add(tracked.material_id)

        if len(preview_digests) != len(set(preview_digests)):
            raise ReconcileError("预览中存在重复摘要附件，归属歧义，停止运行")

        # 反向核对：journal 里曾标记“通过”的材料若预览中消失，说明被人工移除，
        # 不自动补传，标记人工处理。
        for material in self.journal.materials.values():
            if material.material_id in reconciled:
                continue
            if material.state == STATE_PASSED:
                material.state = STATE_NEEDS_HUMAN
                material.note = "此前已关联的附件在预览中消失，疑似人工移除，未自动补传"
        self._save()

    # ------------------------------------------------------------------ 单条上传
    def _upload_one(self, driver: PortalDriver, material: Material) -> None:
        assert self.journal is not None
        tracked = self.journal.materials[material.material_id]

        # 重新计算本地摘要，防止清单生成后文件被替换。
        live_digest = sha256_file(material.path)
        if live_digest != material.digest:
            tracked.state = STATE_NEEDS_HUMAN
            tracked.note = "本地文件摘要与清单记录不一致，停止以防错挂附件"
            self._save()
            raise ReconcileError(
                "本地文件在清单生成后发生变化：",
                items=[f"{material.material_id} 清单 {material.digest[:12]}… / 现 {live_digest[:12]}…"],
            )

        # 替换场景：先移除当前（失败/卡住）的旧 upload_id，并把它记入沿革。
        # 旧面板从 DOM 移除后，其 onchange 迟到结果没有面板可写入，无法复活。
        if tracked.current_upload_id:
            old_id = tracked.current_upload_id
            panels = {p["upload_id"]: p for p in driver.list_panels()}
            if old_id in panels:
                driver.remove_upload(old_id)
            for attempt in tracked.attempts:
                if attempt.upload_id == old_id:
                    attempt.superseded = True
            tracked.note = f"旧 upload_id {old_id} 已被取代，迟到结果不认领"
            tracked.current_upload_id = None
            tracked.state = STATE_REPLACED
            self._save()

        # 表单逐字段设置并回读核对（项目/类型/动态字段）。
        driver.select_project(material.project)
        driver.select_kind(material.kind)
        driver.fill_dynamic_fields(material.kind, material.dynamic_fields)

        panel = driver.choose_file(material.path, material.digest)
        upload_id = panel["upload_id"]
        tracked.current_upload_id = upload_id
        tracked.state = STATE_UPLOADED
        tracked.attempts.append(
            Attempt(
                upload_id=upload_id,
                digest=material.digest,
                path=str(material.path),
                state=panel["state"],
            )
        )
        self._save()

        terminal = driver.wait_terminal(material.digest, upload_id)
        # 终态面板摘要必须仍是本材料；旧面板已被移除，其迟到结果没有面板可认领。
        if terminal["digest"] != material.digest:
            raise ReconcileError(
                "预检终态摘要与材料不一致，疑似迟到结果串单：",
                items=[f"{material.material_id}: {terminal['upload_id']}"],
            )
        if terminal["upload_id"] != upload_id:
            # 门户重建组件复用了 id：记录沿革对齐，不影响摘要归属。
            tracked.note = (
                f"预检期间 upload_id 由门户重建：{upload_id} -> {terminal['upload_id']}"
            )
            upload_id = terminal["upload_id"]
            tracked.current_upload_id = upload_id

        attempt = next(a for a in tracked.attempts if a.upload_id == upload_id)
        attempt.state = terminal["state"]
        if terminal["state"] == PRECHECK_PASSED:
            tracked.state = STATE_PASSED
            tracked.note = "预检通过，可进入草稿附件映射"
        else:
            # 预检失败：不进入草稿映射，保留沿革等待替换，不自动重试未知内容。
            tracked.state = STATE_FAILED
            tracked.note = "预检失败，未关联草稿；请提供替换文件后重新运行"
        self._save()

    def _verify_mapping(self, driver: PortalDriver, passed: dict[str, MaterialState]) -> None:
        """推进前的双向核对。

        - 面板（附件组件）：逐材料恢复项目选择器 + 重渲染后核对摘要与状态，
          并记录门户项目字段的重算漂移，供人工复核；
        - 草稿预览：只承载“预检通过附件”的摘要集合，核对通过项与预览项一致，
          失败项不得进入；
        - 项目归属的权威记录是 RPA journal 的材料沿革，不依赖会被重算的页面字段。
        """
        assert self.journal is not None
        live_by_digest: dict[str, dict] = {}
        for tracked in passed.values():
            driver.select_project(tracked.project)
            # 面板 data-project 只在 render 时写入，切换下拉后需重渲染再核对。
            driver.refresh_panels()
            panel = next(
                (p for p in driver.list_panels() if p["digest"] == tracked.digest),
                None,
            )
            if panel is None:
                raise ReconcileError(
                    "已通过附件在页面上消失：", items=[tracked.material_id]
                )
            if panel["state"] != PRECHECK_PASSED:
                raise ReconcileError(
                    "附件预检状态与沿革不符：",
                    items=[f"{tracked.material_id}={panel['state']}"],
                )
            if panel["project"] != tracked.project:
                # 在材料自身项目上下文重渲染后仍不一致，说明门户状态异常，停止。
                raise ReconcileError(
                    "材料在自身项目上下文中核对仍不一致，停止交人工确认：",
                    items=[
                        f"{tracked.material_id} 材料项目 {tracked.project} / "
                        f"面板项目 {panel['project']}"
                    ],
                )
            live_by_digest[tracked.digest] = panel
            if tracked.current_upload_id != panel["upload_id"]:
                tracked.note = (
                    f"门户重建组件后 upload_id 变化："
                    f"{tracked.current_upload_id} -> {panel['upload_id']}（按摘要重新对齐）"
                )
                tracked.current_upload_id = panel["upload_id"]

        draft_id, preview_digests = driver.passed_preview_digests()
        if draft_id != self.journal.draft_id:
            raise ReconcileError(
                "预览草稿编号与清单不符：", items=[draft_id]
            )
        passed_digests = set(passed)
        if not preview_digests.issubset(passed_digests):
            raise ReconcileError(
                "预览包含未被 RPA 确认通过的附件，停止：",
                items=[digest[:12] for digest in preview_digests - passed_digests],
            )
        if not passed_digests.issubset(preview_digests):
            raise ReconcileError(
                "已通过预检的附件未进入草稿映射，停止：",
                items=[m.material_id for m in passed.values()
                       if m.digest not in preview_digests],
            )
        self._save()

    # ------------------------------------------------------------------ 主流程
    def run(self, resume: bool = False) -> RunReport:
        self.store.acquire_lock(self.manifest.draft_id)
        cancelled = False
        driver: PortalDriver | None = None
        try:
            existing = self.store.load(self.manifest.draft_id)
            if existing is not None and not resume:
                # 显式 run 允许用新清单接管同草稿（典型场景：失败文件替换），
                # 但材料集合必须一致，且按材料编号保留 upload_id 沿革；
                # 草稿上已通过的附件仍以页面/预览为准，不会被静默覆盖。
                self.journal = self._carry_history(existing)
            else:
                self.journal = existing or self._build_fresh_journal()
            # 恢复时校验 journal 与当前清单的材料集合、摘要一致。
            if resume:
                self._assert_journal_matches_manifest()

            driver = PortalDriver(
                portal_url=self.portal_url,
                user_data_dir=self.user_data_dir,
                cancel_event=self.cancel_event,
                headless=self.headless,
            )
            driver.start()

            if resume:
                self._reconcile_with_draft(driver)
            else:
                draft_id = driver.read_draft_id()
                if draft_id != self.journal.draft_id:
                    raise ReconcileError(
                        "门户草稿编号与清单不符：",
                        items=[f"清单 {self.journal.draft_id} / 门户 {draft_id}"],
                    )
                self.journal.verified_draft_id = draft_id
                self._save()

            passed: dict[str, MaterialState] = {}
            for index, material in enumerate(self.manifest.materials):
                tracked = self.journal.materials[material.material_id]
                if tracked.state == STATE_SKIPPED_EXISTING and tracked.current_upload_id:
                    passed[material.digest] = tracked
                    continue
                if tracked.state == STATE_PASSED and tracked.current_upload_id:
                    # 正常运行中断后的续跑：页面仍在同一持久上下文，
                    # 核对面板存在且通过，再跳过。
                    panel = next(
                        (p for p in driver.list_panels()
                         if p["digest"] == tracked.digest),
                        None,
                    )
                    if panel and panel["state"] == PRECHECK_PASSED \
                            and panel["project"] == tracked.project:
                        tracked.current_upload_id = panel["upload_id"]
                        passed[material.digest] = tracked
                        continue
                self._upload_one(driver, material)
                tracked = self.journal.materials[material.material_id]
                if tracked.state == STATE_PASSED:
                    passed[material.digest] = tracked

                # 向导回退：组件可能重建、附件重排，回退后逐项重新核对。
                if self.back_between_materials and index < len(self.manifest.materials) - 1:
                    driver.go_back_and_verify(self.journal.draft_id)
                    self._assert_panels_after_back(driver, passed)

                self._verify_mapping(driver, passed)
                driver.assert_no_publish()

            return self._finalize(driver, cancelled=False)
        except RunCancelled:
            cancelled = True
            if self.journal is not None:
                self._save()
            return self._finalize(driver, cancelled=True)
        finally:
            if driver is not None:
                driver.close()
            self.store.release_lock()

    def _carry_history(self, existing: Journal) -> Journal:
        """显式重跑时把旧 journal 的 upload_id 沿革按材料编号带到新清单。"""
        old_ids = set(existing.materials)
        new_ids = {m.material_id for m in self.manifest.materials}
        if old_ids != new_ids:
            raise ReconcileError(
                "新清单与草稿进度的材料集合不一致，拒绝接管：",
                items=[f"仅旧清单：{sorted(old_ids - new_ids)}",
                       f"仅新清单：{sorted(new_ids - old_ids)}"],
            )
        carried = self._build_fresh_journal()
        carried.verified_draft_id = existing.verified_draft_id
        for material in self.manifest.materials:
            old = existing.materials[material.material_id]
            fresh = carried.materials[material.material_id]
            # 沿革完整保留；新清单摘要不同意味着替换文件，旧尝试全部标记 superseded。
            fresh.attempts = [
                Attempt(
                    upload_id=a.upload_id,
                    digest=a.digest,
                    path=a.path,
                    state=a.state,
                    superseded=a.superseded or a.digest != material.digest,
                )
                for a in old.attempts
            ]
            if old.digest == material.digest and old.current_upload_id:
                # 内容未变：保留当前关联，后续流程按页面状态决定跳过或重传。
                fresh.current_upload_id = old.current_upload_id
                fresh.state = old.state
                fresh.note = "沿用既有进度：" + old.note
            elif old.current_upload_id:
                stale = old.current_upload_id
                for attempt in fresh.attempts:
                    if attempt.upload_id == stale:
                        attempt.superseded = True
                fresh.state = STATE_REPLACED
                fresh.note = (
                    f"清单文件已替换，旧 upload_id {stale} 标记为已取代，"
                    "其迟到结果不认领"
                )
        return carried

    def _assert_journal_matches_manifest(self) -> None:
        assert self.journal is not None
        if {m.material_id for m in self.journal.materials.values()} != \
                {m.material_id for m in self.manifest.materials}:
            raise ReconcileError(
                "进度日志的材料集合与当前清单不一致，拒绝恢复",
                items=list(self.journal.materials),
            )
        for material in self.manifest.materials:
            tracked = self.journal.materials[material.material_id]
            if tracked.digest != material.digest:
                raise ReconcileError(
                    "进度日志中的文件摘要与当前清单不一致，拒绝恢复以防重复/错传：",
                    items=[material.material_id],
                )

    def _assert_panels_after_back(
        self, driver: PortalDriver, passed: dict[str, MaterialState]
    ) -> None:
        """回退重建组件后按摘要+项目核对：已通过项仍在，失败/取代项不混入。

        门户的项目下拉是全局单值，回退会按当前选择器重算面板的 data-project。
        因此核对前先按材料逐个恢复选择器，再读取面板真实属性，
        项目无法恢复一致即停止交人工，绝不带着错误关联继续推进。
        """
        assert self.journal is not None
        for tracked in passed.values():
            driver.select_project(tracked.project)
            driver.refresh_panels()
            panels = driver.list_panels()
            live = {p["digest"]: p for p in panels}
            panel = live.get(tracked.digest)
            if panel is None:
                raise ReconcileError(
                    "向导回退后已通过附件丢失：", items=[tracked.material_id]
                )
            if panel["state"] != PRECHECK_PASSED:
                raise ReconcileError(
                    "向导回退后附件预检状态异常：",
                    items=[f"{tracked.material_id}={panel['state']}"],
                )
            if panel["project"] != tracked.project:
                raise ReconcileError(
                    "回退后在材料自身项目上下文中核对仍不一致，停止交人工确认：",
                    items=[
                        f"{tracked.material_id} 材料项目 {tracked.project} / "
                        f"面板项目 {panel['project']}"
                    ],
                )
        panels = driver.list_panels()
        passed_digests = set(passed)
        for tracked in self.journal.materials.values():
            if tracked.digest in passed_digests:
                continue
            # 只核对该材料“当前 upload_id”：失败/替换中的材料当前面板不得
            # 显示通过。沿革里的旧 upload_id 已被取代，不参与映射判断；
            # 替换后新摘要若已通过，会在后续循环进入 passed 集合。
            if not tracked.current_upload_id:
                continue
            panel = next(
                (p for p in panels if p["upload_id"] == tracked.current_upload_id),
                None,
            )
            if panel is not None and panel["state"] == PRECHECK_PASSED:
                raise ReconcileError(
                    "未通过/已替换材料的当前附件回退后混入映射：",
                    items=[tracked.material_id],
                )

    # ------------------------------------------------------------------ 报告
    def _finalize(self, driver: PortalDriver | None, cancelled: bool) -> RunReport:
        assert self.journal is not None
        preview_path = self.output_dir / f"preview-{self.journal.draft_id}.json"
        screenshot_path = self.output_dir / f"preview-{self.journal.draft_id}.png"
        materials: list[MaterialReport] = []
        needs_human: list[str] = []
        passed_ids: set[str] = set()

        if driver is not None and not cancelled:
            try:
                # 门户预览的项目字段会被全局下拉重算，只取通过附件的摘要集合，
                # 项目归属以 journal 的材料沿革为权威并在报告中提示人工复核。
                draft_id, preview_digests = driver.passed_preview_digests()
                preview = {
                    "draft_id": draft_id,
                    "attachments": [
                        {
                            "material_id": material.material_id,
                            "project": material.project,
                            "kind": material.kind,
                            "summary": material.summary,
                            "digest": material.digest,
                            "upload_id": self.journal.materials[
                                material.material_id
                            ].current_upload_id,
                            "state": PRECHECK_PASSED,
                        }
                        for material in self.manifest.materials
                        if self.journal.materials[material.material_id].state
                        in (STATE_PASSED, STATE_SKIPPED_EXISTING)
                        and material.digest in preview_digests
                    ],
                }
                if {a["digest"] for a in preview["attachments"]} != preview_digests:
                    raise ReconcileError(
                        "预览摘要集合与 journal 通过项不一致，停止交人工确认",
                        items=[str(preview_digests)],
                    )
            except RunCancelled:
                preview = {}
            driver.screenshot(screenshot_path)
            driver.assert_no_publish()
        else:
            # 取消场景：不再驱动页面，只落盘 journal 视角的空预览占位。
            preview = {}
        preview_path.write_text(
            json.dumps(preview, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        for material in self.manifest.materials:
            tracked = self.journal.materials[material.material_id]
            linked = bool(
                tracked.current_upload_id
                and tracked.state in (STATE_PASSED, STATE_SKIPPED_EXISTING)
                and tracked.digest in {a["digest"] for a in preview.get("attachments", [])}
            )
            if tracked.state in (STATE_PASSED, STATE_SKIPPED_EXISTING):
                assert tracked.current_upload_id
                passed_ids.add(tracked.current_upload_id)
            if tracked.state in (STATE_FAILED, STATE_NEEDS_HUMAN):
                needs_human.append(tracked.material_id)
            field_validation = (
                "通过：" + ",".join(
                    f"{k}={v}" for k, v in material.dynamic_fields.items()
                )
            )
            precheck = {
                STATE_PASSED: PRECHECK_PASSED,
                STATE_SKIPPED_EXISTING: PRECHECK_PASSED + "（恢复确认）",
                STATE_FAILED: PRECHECK_FAILED,
                STATE_UPLOADED: "预检中（运行中断）",
                STATE_REPLACED: "已替换待重传",
                STATE_PENDING: "未上传",
                STATE_NEEDS_HUMAN: "待人工",
            }.get(tracked.state, tracked.state)
            materials.append(MaterialReport(
                material_id=tracked.material_id,
                project=tracked.project,
                kind=tracked.kind,
                path=tracked.path,
                digest=tracked.digest,
                state=tracked.state,
                upload_id=tracked.current_upload_id,
                precheck=precheck,
                field_validation=field_validation,
                draft_linked=linked,
                note=tracked.note,
                attempts=[asdict(a) for a in tracked.attempts],
            ))

        self.journal.preview_path = str(preview_path)
        self.journal.preview_url = f"{self.portal_url}/#draft-{self.journal.draft_id}"
        self._save()

        publish_count = int(driver.page.evaluate("window.__rpaPublishBlocked || 0")) \
            if driver is not None and driver.page is not None else 0
        report = RunReport(
            draft_id=self.journal.draft_id,
            portal_url=self.portal_url,
            preview_url=self.journal.preview_url,
            preview_path=str(preview_path),
            publish_count=publish_count,
            materials=materials,
            needs_human=needs_human,
            cancelled=cancelled,
        )
        report_path = self.output_dir / f"report-{self.journal.draft_id}.json"
        report_path.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return report
