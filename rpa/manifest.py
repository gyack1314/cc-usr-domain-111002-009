"""资料清单的加载与上传前校验。

清单结构（JSON）::

    {
      "draft_id": "DRAFT-17",
      "base_dir": "..",                    // 可选，相对路径基于它解析
      "materials": [
        {
          "material_id": "MAT-17",
          "path": "materials/north/report.txt",
          "kind": "report",
          "project": "PR-17",
          "summary": "北区检测文本",
          "fields": {"report_date": "2026-11-03"}
        }
      ]
    }

关键约束：
- 材料类型对应的动态必要字段缺失时，在上传前直接报错，绝不自动填未知值。
- 路径不可读、编号重复、摘要/项目/类型缺失同样属于上传前错误。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .digestutil import sha256_file
from .errors import ManifestError

# 与门户 web/index.html 的选项及 web/app.js 的动态字段保持一致。
# value -> (中文名称, 动态字段 {字段名: 中文标签})
SUPPORTED_KINDS: dict[str, tuple[str, dict[str, str]]] = {
    "report": ("检测报告", {"report_date": "检测日期"}),
    "license": ("许可材料", {"license_id": "许可编号"}),
}

SUPPORTED_PROJECTS = {"PR-17": "北区检测", "PR-29": "南区检测"}


@dataclass
class Material:
    material_id: str
    path: Path
    kind: str
    project: str
    summary: str
    dynamic_fields: dict[str, str] = field(default_factory=dict)
    digest: str = ""
    size: int = 0

    @property
    def required_fields(self) -> dict[str, str]:
        return SUPPORTED_KINDS[self.kind][1]


@dataclass
class Manifest:
    draft_id: str
    materials: list[Material]
    source: Path


def _collect_errors(raw: dict, base_dir: Path) -> list[str]:
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["清单根节点必须是 JSON 对象"]

    draft_id = raw.get("draft_id")
    if not isinstance(draft_id, str) or not draft_id.strip():
        errors.append("缺少 draft_id（草稿编号）")

    entries = raw.get("materials")
    if not isinstance(entries, list) or not entries:
        errors.append("materials 必须是非空列表")
        return errors

    seen_ids: set[str] = set()
    for index, entry in enumerate(entries):
        prefix = f"materials[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{prefix} 必须是对象")
            continue

        material_id = entry.get("material_id")
        if not isinstance(material_id, str) or not material_id.strip():
            errors.append(f"{prefix} 缺少 material_id")
        elif material_id in seen_ids:
            errors.append(f"{prefix} material_id 重复：{material_id}")
        else:
            seen_ids.add(material_id)
            prefix = f"{prefix} {material_id}"

        for key in ("kind", "project", "summary"):
            if not isinstance(entry.get(key), str) or not entry[key].strip():
                errors.append(f"{prefix} 缺少必要字段 {key}")

        kind = entry.get("kind")
        if isinstance(kind, str) and kind not in SUPPORTED_KINDS:
            errors.append(
                f"{prefix} 不支持的材料类型 {kind!r}，"
                f"可选：{', '.join(SUPPORTED_KINDS)}"
            )

        project = entry.get("project")
        if isinstance(project, str) and project not in SUPPORTED_PROJECTS:
            errors.append(
                f"{prefix} 不支持的所属项目 {project!r}，"
                f"可选：{', '.join(SUPPORTED_PROJECTS)}"
            )

        dynamic_fields = entry.get("fields", {})
        if not isinstance(dynamic_fields, dict):
            errors.append(f"{prefix} fields 必须是对象")
            dynamic_fields = {}

        # 动态必要字段校验：缺任一字段即上传前报错，不自动填值。
        if isinstance(kind, str) and kind in SUPPORTED_KINDS:
            for field_name, label in SUPPORTED_KINDS[kind][1].items():
                value = dynamic_fields.get(field_name)
                if not isinstance(value, str) or not value.strip():
                    errors.append(
                        f"{prefix} 类型 {kind} 缺少必要动态字段 "
                        f"fields.{field_name}（{label}）；"
                        "不得自动填写未知值，请在清单中补全"
                    )
                else:
                    dynamic_fields[field_name] = value.strip()

        raw_path = entry.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            errors.append(f"{prefix} 缺少本地路径 path")
            continue
        path = (base_dir / raw_path).resolve()
        if not path.is_file():
            errors.append(f"{prefix} 本地文件不存在或不可读：{path}")

    return errors


def load_manifest(manifest_path: str | Path) -> Manifest:
    """加载并完整校验清单；任一问题都抛出 ManifestError，不执行任何上传。"""
    manifest_path = Path(manifest_path).resolve()
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ManifestError(f"清单文件不存在：{manifest_path}")
    except json.JSONDecodeError as exc:
        raise ManifestError(f"清单 JSON 不合法：{exc}")

    base_dir = manifest_path.parent
    configured_base = raw.get("base_dir") if isinstance(raw, dict) else None
    if isinstance(configured_base, str) and configured_base.strip():
        base_dir = (base_dir / configured_base).resolve()

    errors = _collect_errors(raw, base_dir)
    if errors:
        raise ManifestError(
            f"清单校验失败，共 {len(errors)} 个问题，未执行任何上传：",
            items=errors,
        )

    materials: list[Material] = []
    for entry in raw["materials"]:
        path = (base_dir / entry["path"]).resolve()
        materials.append(
            Material(
                material_id=entry["material_id"],
                path=path,
                kind=entry["kind"],
                project=entry["project"],
                summary=entry["summary"].strip(),
                dynamic_fields={
                    key: value.strip()
                    for key, value in entry.get("fields", {}).items()
                    if isinstance(value, str)
                },
                digest=sha256_file(path),
                size=path.stat().st_size,
            )
        )

    # 同名且同内容的文件无法在同草稿中区分归属，清单层面直接拒绝歧义；
    # 同名异内容正是本 RPA 要支持的场景，不同名同内容允许（upload_id 仍唯一）。
    seen: dict[tuple[str, str], str] = {}
    for material in materials:
        key = (material.path.name, material.digest)
        owner = seen.get(key)
        if owner is not None:
            raise ManifestError(
                "清单存在同名且内容完全相同的文件，无法在同草稿中区分归属：",
                items=[
                    f"{owner} 与 {material.material_id} 文件名与摘要均相同"
                    f"（{material.digest[:12]}…）"
                ],
            )
        seen[key] = material.material_id

    return Manifest(
        draft_id=raw["draft_id"].strip(),
        materials=materials,
        source=manifest_path,
    )
