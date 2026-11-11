"""Playwright 浏览器驱动：所有门户操作都走真实 DOM 与文件 input。

认领规则（核心）：
- 门户为每次文件选择分配 upload_id，预检结果按 upload_id 异步返回。
- RPA 不按列表位置或文件名认领，而是先在本地计算 SHA-256，
  再用面板上的 data-digest 属性找到本次选择产生的面板，读取其 upload_id。
- 预检未到终态只等待；终态以面板文本“通过 / 预检失败”为准。
- 已被取代（superseded）的 upload_id 即使迟到成功也不会再被认领。

发布护栏：本驱动初始化即注入脚本，点击“最终发布”会被阻止并抛错，
保证 RPA 任何路径都不可能完成发布。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from playwright.sync_api import (
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    expect,
    sync_playwright,
)

from .errors import PortalError, RunCancelled

PRECHECK_PASSED = "通过"
PRECHECK_FAILED = "预检失败"
PRECHECK_RUNNING = "预检中"
TERMINAL_STATES = {PRECHECK_PASSED, PRECHECK_FAILED}

# 轮询间隔短于预检周期，且每次都检查取消事件，避免固定秒数等待。
_POLL_MS = 120


class PortalDriver:
    def __init__(
        self,
        portal_url: str,
        user_data_dir: str | Path,
        cancel_event: threading.Event | None = None,
        headless: bool = True,
        default_timeout_ms: int = 30_000,
    ):
        self.portal_url = portal_url.rstrip("/")
        self.user_data_dir = Path(user_data_dir)
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.cancel_event = cancel_event or threading.Event()
        self.headless = headless
        self.default_timeout_ms = default_timeout_ms
        self._playwright: Playwright | None = None
        self._context = None
        self.page: Page | None = None
        self.publish_blocked = 0

    # ------------------------------------------------------------------ 生命周期
    def __enter__(self) -> "PortalDriver":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        # 持久化上下文：草稿附件保存在门户 origin 的 localStorage，
        # 关闭页面后重开同一草稿仍能看到既有附件。
        self._context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.user_data_dir),
            headless=self.headless,
            args=["--no-sandbox"],
            accept_downloads=True,
            viewport={"width": 1280, "height": 900},
        )
        self.page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self.page.set_default_timeout(self.default_timeout_ms)
        self._page_errors: list[str] = []
        self.page.on("pageerror", self._on_page_error)
        self._install_publish_guard(self.page)
        self.page.goto(self.portal_url)
        # 等待模块脚本初始化完成（中文加载提示消失），不用固定秒数。
        expect(self.page.locator("#status")).not_to_have_text("页面加载中")

    def close(self) -> None:
        try:
            if self._context is not None:
                # 保存进度后关闭页面：localStorage 随持久化上下文落盘。
                self._context.close()
        finally:
            self._context = None
            self.page = None
            if self._playwright is not None:
                self._playwright.stop()
                self._playwright = None

    def _install_publish_guard(self, page: Page) -> None:
        """在页面所有脚本前注入发布拦截，点击发布按钮直接阻断并计数。"""
        page.add_init_script(
            """
            window.__rpaPublishBlocked = 0;
            document.addEventListener('click', function (event) {
                const target = event.target && event.target.closest
                    ? event.target.closest('#publish') : null;
                if (target) {
                    event.preventDefault();
                    event.stopPropagation();
                    event.stopImmediatePropagation();
                    window.__rpaPublishBlocked += 1;
                    throw new Error('RPA 护栏：最终发布只能由工作人员手动点击');
                }
            }, true);
            """
        )

    def _on_page_error(self, error) -> None:
        # 页面自身脚本错误需要记录，但发布护栏抛出的错误是预期拦截，不算页面故障。
        message = str(error)
        if "RPA 护栏" in message:
            return
        self._page_errors.append(message)

    def _raise_if_cancelled(self) -> None:
        if self._page_errors:
            raise PortalError(
                "门户页面脚本错误：" + "；".join(self._page_errors),
                items=list(self._page_errors),
            )
        if self.cancel_event.is_set():
            raise RunCancelled("收到取消信号，停止操作并保留草稿")

    # ------------------------------------------------------------------ 草稿与表单
    def read_draft_id(self) -> str:
        self._raise_if_cancelled()
        draft_id = self.page.locator("#draft").inner_text().strip()
        if not draft_id:
            raise PortalError("门户未返回草稿编号")
        return draft_id

    def select_project(self, project: str) -> None:
        self._raise_if_cancelled()
        if project not in set(self.page.locator("#project").evaluate(
            "el => [...el.options].map(o => o.value)"
        )):
            raise PortalError(f"门户不存在项目 {project!r}", items=[project])
        self.page.select_option("#project", project)
        # 推进后回读确认，防止组件重建造成选择漂移。
        actual = self.page.locator("#project").input_value()
        if actual != project:
            raise PortalError(f"项目选择核对失败：期望 {project}，实际 {actual}")

    def refresh_panels(self) -> None:
        """门户面板的 data-project 只在 render 时写入；切换项目下拉后需重渲染，
        才能读到反映当前选择器值的面板属性。回退按钮本身即触发 render。"""
        self._raise_if_cancelled()
        self.page.click("#back")
        expect(self.page.locator("#status")).not_to_have_text("页面加载中")

    def select_kind(self, kind: str) -> None:
        self._raise_if_cancelled()
        self.page.select_option("#kind", kind)
        # 动态字段随类型重建，等对应输入框出现再继续。
        field_id = "#report-date" if kind == "report" else "#license-id"
        self.page.locator(field_id).wait_for(state="visible")

    def fill_dynamic_fields(self, kind: str, values: dict[str, str]) -> None:
        """只填写清单明确给出的动态字段，不填任何未知值。"""
        self._raise_if_cancelled()
        mapping = {"report": ("#report-date", "report_date"),
                   "license": ("#license-id", "license_id")}
        selector, field_name = mapping[kind]
        if field_name not in values or not values[field_name].strip():
            # 理论上清单校验已拦截，这里是驱动层的最后防线。
            raise PortalError(f"动态字段 {field_name} 为空，拒绝自动填写未知值")
        self.page.fill(selector, values[field_name])
        if self.page.input_value(selector) != values[field_name]:
            raise PortalError(f"动态字段 {field_name} 填写后核对不一致")

    # ------------------------------------------------------------------ 附件面板
    def list_panels(self) -> list[dict]:
        """读取当前全部附件面板（upload_id / digest / project / state / name）。"""
        self._raise_if_cancelled()
        panels = self.page.locator("[data-upload-id]")
        result: list[dict] = []
        for index in range(panels.count()):
            panel = panels.nth(index)
            result.append({
                "upload_id": panel.get_attribute("data-upload-id"),
                "digest": panel.get_attribute("data-digest"),
                "project": panel.get_attribute("data-project"),
                "state": panel.locator(".check-state").inner_text().strip(),
                "name": panel.locator("p").first.inner_text().split(" ", 1)[0],
            })
        return result

    def panel_by_digest(self, digest: str) -> dict | None:
        for panel in self.list_panels():
            if panel["digest"] == digest:
                return panel
        return None

    def wait_terminal(self, digest: str, current_upload_id: str) -> dict:
        """等待本次摘要对应的面板到达预检终态。

        current_upload_id 是本次选择记录的 upload_id；若面板 id 在等待期间
        变化（门户组件重建），以摘要 + 非终态不匹配为准重新对齐。
        已被取代的旧面板由调用方在选择前移除，迟到结果因面板不存在而无法认领。
        """
        deadline_cycles = max(1, self.default_timeout_ms // _POLL_MS)
        for _ in range(deadline_cycles):
            self._raise_if_cancelled()
            panel = self.panel_by_digest(digest)
            if panel is not None and panel["state"] in TERMINAL_STATES:
                return panel
            self.page.wait_for_timeout(_POLL_MS)
        raise PortalError(
            f"预检在 {self.default_timeout_ms}ms 内未返回终态（摘要 {digest[:12]}…，"
            f"本次 upload_id {current_upload_id}）"
        )

    # ------------------------------------------------------------------ 上传与替换
    def choose_file(self, file_path: str | Path, expected_digest: str) -> dict:
        """通过浏览器 file input 选择文件，并凭摘要（不是位置/文件名）认领面板。

        回传包含 upload_id 与预检状态的面板字典。
        """
        self._raise_if_cancelled()
        input_handle = self.page.locator("#files")
        # 真实文件选择事件：set_input_files 作用于 <input type=file>，
        # 门户的 onchange 会被正常触发。
        input_handle.set_input_files(str(file_path))
        # 等待出现新面板，且其摘要必须等于本地预计算摘要。
        try:
            self.page.wait_for_function(
                """(digest) => {
                    const nodes = [...document.querySelectorAll('[data-upload-id]')];
                    return nodes.some(n => n.dataset.digest === digest);
                }""",
                arg=expected_digest,
                timeout=self.default_timeout_ms,
            )
        except PlaywrightTimeoutError:
            raise PortalError(
                "文件选择后未在门户发现对应新附件，"
                "请确认文件由 #files input 接收（摘要 "
                f"{expected_digest[:12]}…）"
            )
        panel = self.panel_by_digest(expected_digest)
        if panel is None:
            raise PortalError("附件认领失败：未找到本次选择产生的新面板")
        # 摘要由本地预计算且清单拒绝同名同内容，因此摘要命中即本次选择；
        # 不把 upload_id 与 before 集合比较，避免门户组件重建后的 id 复用造成误判。
        return panel

    def remove_upload(self, upload_id: str) -> None:
        """通过门户“移除附件”按钮移除指定 upload_id 的面板。"""
        self._raise_if_cancelled()
        button = self.page.locator(f'[data-remove="{upload_id}"]')
        button.click()
        self.page.locator(f'[data-upload-id="{upload_id}"]').wait_for(
            state="detached", timeout=self.default_timeout_ms
        )

    def go_back_and_verify(self, expected_draft_id: str) -> list[dict]:
        """触发向导回退（门户会重建组件并反转附件顺序），然后重新核对映射。"""
        self._raise_if_cancelled()
        self.page.click("#back")
        expect(self.page.locator("#status")).not_to_have_text("页面加载中")
        draft_id = self.read_draft_id()
        if draft_id != expected_draft_id:
            raise PortalError(
                f"回退后草稿编号漂移：期望 {expected_draft_id}，实际 {draft_id}"
            )
        return self.list_panels()

    # ------------------------------------------------------------------ 预览与护栏
    def open_preview(self) -> dict:
        """点击“草稿预览”，解析预览 JSON；只含预检通过的附件。"""
        self._raise_if_cancelled()
        self.page.click("#preview")
        raw = self.page.locator("#preview-result").inner_text()
        try:
            preview = json.loads(raw)
        except json.JSONDecodeError:
            raise PortalError(f"草稿预览内容无法解析：{raw[:200]}")
        self.publish_blocked = int(self.page.evaluate("window.__rpaPublishBlocked || 0"))
        return preview

    def passed_preview_digests(self) -> tuple[str, set[str]]:
        """返回 (draft_id, 预览中通过附件的摘要集合)。

        门户项目下拉是全局单值，预览里的 project 字段会被重算为当前选择器，
        不具备项目归属权威性；因此这里只输出摘要集合，
        项目归属由 RPA 的材料沿革（journal）权威记录并在报告中提示人工复核。
        """
        preview = self.open_preview()
        digests = {a["digest"] for a in preview.get("attachments", [])}
        return preview.get("draft_id", ""), digests

    def assert_no_publish(self) -> None:
        blocked = int(self.page.evaluate("window.__rpaPublishBlocked || 0"))
        self.publish_blocked = blocked
        if blocked:
            raise PortalError(f"发布护栏被触发 {blocked} 次，RPA 不得触碰发布")

    def screenshot(self, path: str | Path) -> None:
        self.page.screenshot(path=str(path), full_page=True)
