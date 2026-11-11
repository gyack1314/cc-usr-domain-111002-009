"""命令行入口：run / resume / validate。

示例
----
python -m rpa.cli validate manifests/manifests.json
python -m rpa.cli run --portal http://127.0.0.1:8090 --manifest manifests/manifest.json
python -m rpa.cli resume --portal http://127.0.0.1:8090 --manifest manifests/manifest.json
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from .errors import ManifestError, RpaError
from .manifest import SUPPORTED_KINDS, load_manifest
from .runner import RunReport, UploadRunner

_STATE_DIR = Path("state")
_OUTPUT_DIR = Path("outputs")
_PROFILE_DIR = Path("state/browser-profile")


def _print_report(report: RunReport) -> None:
    print("=" * 78)
    print(f"草稿编号：{report.draft_id}    门户：{report.portal_url}")
    print(f"预览链接：{report.preview_url}")
    print(f"预览快照：{report.preview_path}")
    print(f"运行是否被取消：{'是（已上传未发布草稿保留）' if report.cancelled else '否'}")
    print("-" * 78)
    header = ("材料编号", "项目", "类型", "预检", "字段校验", "草稿关联", "upload_id")
    rows = [header]
    for item in report.materials:
        rows.append((
            item.material_id,
            item.project,
            item.kind,
            item.precheck,
            item.field_validation,
            "是" if item.draft_linked else "否",
            item.upload_id or "-",
        ))
    widths = [max(_display_width(row[i]) for row in rows) for i in range(len(header))]
    for row_index, row in enumerate(rows):
        print("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(row)))
        if row_index == 0:
            print("  ".join("-" * widths[i] for i in range(len(header))))
    print("-" * 78)
    for item in report.materials:
        print(f"[{item.material_id}] 状态={item.state} 摘要={item.digest[:16]}…")
        if item.note:
            print(f"    说明：{item.note}")
    print("-" * 78)
    if report.needs_human:
        print("需要人工处理的条目：" + ", ".join(report.needs_human))
    else:
        print("需要人工处理的条目：无")
    # 发布计数必须始终为零；非零直接以错误码退出。
    print(f"最终发布计数：{report.publish_count}（RPA 全程不得发布）")
    print("=" * 78)


def _display_width(text: str) -> int:
    width = 0
    for char in str(text):
        width += 2 if ord(char) > 0x2E7F else 1
    return width


def _pad(text: str, width: int) -> str:
    return str(text) + " " * max(0, width - _display_width(text))


def _build_runner(args, manifest) -> tuple[UploadRunner, threading.Event]:
    cancel_event = threading.Event()

    def _handle_signal(signum, frame):
        cancel_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    runner = UploadRunner(
        manifest=manifest,
        portal_url=args.portal,
        state_dir=args.state_dir,
        output_dir=args.output_dir,
        user_data_dir=args.profile_dir,
        cancel_event=cancel_event,
        headless=not args.headed,
        back_between_materials=not args.no_back,
    )
    return runner, cancel_event


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m rpa.cli",
        description="合规资料草稿上传 RPA（不执行最终发布）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--manifest", required=True, help="资料清单 JSON 路径")
        p.add_argument("--portal", default="http://127.0.0.1:8090", help="门户地址")
        p.add_argument("--state-dir", default=str(_STATE_DIR), help="进度日志目录")
        p.add_argument("--output-dir", default=str(_OUTPUT_DIR), help="报告与预览目录")
        p.add_argument("--profile-dir", default=str(_PROFILE_DIR),
                       help="浏览器持久化目录（保存草稿 localStorage）")
        p.add_argument("--headed", action="store_true", help="显示浏览器窗口")
        p.add_argument("--no-back", action="store_true",
                       help="关闭材料间的向导回退核对（默认开启）")

    p_validate = sub.add_parser("validate", help="只做清单与动态字段校验，不打开浏览器")
    p_validate.add_argument("--manifest", required=True)

    p_run = sub.add_parser("run", help="开始上传；已有进度日志时拒绝执行")
    add_common(p_run)

    p_resume = sub.add_parser("resume", help="恢复：先核对同一草稿与既有附件，再补齐缺项")
    add_common(p_resume)

    args = parser.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        if args.command == "validate":
            print(f"清单校验通过，草稿 {manifest.draft_id}，共 {len(manifest.materials)} 条材料")
            for material in manifest.materials:
                labels = SUPPORTED_KINDS[material.kind][1]
                fields = "，".join(
                    f"{labels[key]}={material.dynamic_fields[key]}"
                    for key in labels
                )
                print(
                    f"  {material.material_id} | {material.project} | {material.kind} | "
                    f"{material.path.name} | 摘要 {material.digest[:12]}… | {fields}"
                )
            return 0

        runner, _event = _build_runner(args, manifest)
        report = runner.run(resume=(args.command == "resume"))
        _print_report(report)
        if report.publish_count != 0:
            return 3
        return 0
    except ManifestError as exc:
        print(f"清单错误：{exc.message}", file=sys.stderr)
        for item in exc.items:
            print(f"  - {item}", file=sys.stderr)
        return 2
    except RpaError as exc:
        print(f"RPA 中止：{exc.message}", file=sys.stderr)
        for item in exc.items:
            print(f"  - {item}", file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"文件不存在：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
